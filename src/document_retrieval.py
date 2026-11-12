"""RAG 检索层：问题生成查询向量，从已有 Qdrant 索引返回正文与出处。

同时提供普通 Python 入口和异步 LangChain Tool。配置与网络连接延迟到实际
调用时创建，因此导入 Agent 不会立即访问数据库或要求填写 Embedding 密钥。
"""

from __future__ import annotations

import asyncio
import logging
import math
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.embeddings import Embeddings
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from openai import APIError
from pydantic import BaseModel, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from embedding_client import EmbeddingSettings, create_embedding_http_client, create_embeddings
from observability.metrics import operation
from observability.stages import RETRIEVE, stage

if TYPE_CHECKING:
    from qdrant_client import QdrantClient

    from qdrant_store import QdrantSettings

logger = logging.getLogger(__name__)


class RetrievalSettings(BaseSettings):
    """限制进入模型上下文的片段数与正文长度，阈值需后续通过评测校准。"""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent / ".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )
    RAG_TOP_K: int = Field(default=3, ge=1, le=5)
    RAG_SCORE_THRESHOLD: float = Field(default=0.45, ge=-1, le=1)
    RAG_MAX_CHARS: int = Field(default=1200, ge=100, le=3000)


class SearchInput(BaseModel):
    """模型只能提交一个有限长度的问题，不能指定数据库地址或任意资源。"""

    query: str = Field(min_length=1, max_length=1000, description="包含故障现象与关键词的检索问题")


class SearchHit(BaseModel):
    """保留可引用信息；score 是相似度分数，不是结论正确的概率。"""

    chunk_id: str
    document_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=500)
    source_url: str = Field(pattern=r"^https?://", max_length=2048)
    content: str = Field(min_length=1)
    score: float
    truncated: bool = False


class RetrievalError(RuntimeError):
    """可向工具反馈的已知错误；不携带服务原始响应、正文或密钥。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def query_index(
    query: str,
    client: QdrantClient,
    embeddings: Embeddings,
    embedding_config: EmbeddingSettings,
    qdrant_config: QdrantSettings,
    retrieval_config: RetrievalSettings,
    *,
    tenant_id: str | None = None,
    document_ids: list[str] | None = None,
) -> list[SearchHit]:
    """可注入客户端的检索核心，便于用真实本地 Qdrant 和假向量做离线测试。"""
    from qdrant_client import models

    query = SearchInput(query=query).query.strip()
    if not query:
        raise RetrievalError("invalid_query", "检索问题不能只有空白。")
    if document_ids == []:
        return []
    info = client.get_collection(qdrant_config.QDRANT_COLLECTION)
    params = info.config.params.vectors
    expected = {
        "index_schema": "dev-issue-documents-v1",
        "embedding_model": embedding_config.EMBEDDING_MODEL,
        "embedding_base_url": str(embedding_config.EMBEDDING_BASE_URL).rstrip("/"),
    }
    # 先检查模型身份，再请求 Embedding。查询向量与文档向量必须在同一向量空间。
    if (
        info.config.metadata != expected
        or not isinstance(params, models.VectorParams)
        or params.distance != models.Distance.COSINE
    ):
        raise RetrievalError(
            "incompatible_index", "索引与当前 Embedding 配置不匹配，请检查模型或 Collection。"
        )
    vector = embeddings.embed_query(query)
    if (
        len(vector) != params.size
        or not all(math.isfinite(value) for value in vector)
        or not any(vector)
    ):
        raise RetrievalError("invalid_vector", "查询向量的维度或数值不合法。")
    points = client.query_points(
        collection_name=qdrant_config.QDRANT_COLLECTION,
        query=vector,
        limit=retrieval_config.RAG_TOP_K,
        score_threshold=retrieval_config.RAG_SCORE_THRESHOLD,
        with_payload=True,
        with_vectors=False,
        query_filter=models.Filter(
            must=[
                models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id))
                if tenant_id
                else models.IsEmptyCondition(is_empty=models.PayloadField(key="tenant_id"))
            ]
            + (
                [models.FieldCondition(key="document_id", match=models.MatchAny(any=document_ids))]
                if document_ids is not None
                else []
            )
        ),
    ).points
    hits = []
    for point in points:
        payload = point.payload or {}
        # 数据库返回的是外部数据，model_validate 会在字段缺失时明确报错。
        hit = SearchHit.model_validate(
            {
                "chunk_id": str(point.id),
                "document_id": payload.get("document_id"),
                "title": payload.get("title"),
                "source_url": payload.get("source_url"),
                "content": payload.get("content"),
                "score": point.score,
            }
        )
        # 不返回完整向量；过长正文也不能无限占用模型的上下文窗口。
        hit.truncated = len(hit.content) > retrieval_config.RAG_MAX_CHARS
        hit.content = hit.content[: retrieval_config.RAG_MAX_CHARS]
        hits.append(hit)
    logger.info(
        "文档检索完成：命中 %d 段，Collection=%s", len(hits), qdrant_config.QDRANT_COLLECTION
    )
    return hits


def search_documents(
    query: str, *, tenant_id: str | None = None, document_ids: list[str] | None = None
) -> list[SearchHit]:
    """创建并关闭每次调用的连接；只读已有索引，不自动建库或修改文档。"""
    if document_ids == []:
        SearchInput(query=query)
        return []
    try:
        # Qdrant 是可选依赖；未安装时普通对话仍能启动，实际检索再给出明确提示。
        from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

        from qdrant_store import QdrantSettings, create_qdrant_client
    except ModuleNotFoundError as exc:
        raise RetrievalError("missing_dependency", "缺少检索依赖，请安装项目 rag 依赖组。") from exc
    try:
        embedding_config = EmbeddingSettings()
        qdrant_config = QdrantSettings()
        retrieval_config = RetrievalSettings()
        with (
            create_embedding_http_client(embedding_config) as http_client,
            closing(create_qdrant_client(qdrant_config)) as client,
        ):
            return query_index(
                query,
                client,
                create_embeddings(embedding_config, http_client=http_client),
                embedding_config,
                qdrant_config,
                retrieval_config,
                tenant_id=tenant_id,
                document_ids=document_ids,
            )
    except ValidationError as exc:
        raise RetrievalError(
            "invalid_data_or_config", "检索配置或索引片段格式不合法，请检查后台。"
        ) from exc
    except APIError as exc:
        raise RetrievalError(
            "embedding_unavailable", "查询向量生成失败，请检查 Embedding 网络、余额与配置。"
        ) from exc
    except (ResponseHandlingException, UnexpectedResponse) as exc:
        raise RetrievalError(
            "qdrant_unavailable", "知识库不可用，请检查 Qdrant 服务、Collection 和认证。"
        ) from exc


@tool(args_schema=SearchInput)
async def search_feishu_documents(query: str, config: RunnableConfig) -> dict:
    """检索已导入的飞书研发文档；遇到故障排查或知识库问题时使用，返回正文与来源链接。

    查询应包含问题现象与关键词；资料只是排查证据，不能证明当前故障根因。
    只搜索已入库文档，不会实时读取整个飞书知识库、搜索 Issue 或执行命令。
    """
    # retrieve 阶段记录命中数、最高分与错误码；不记录查询原文与片段正文。
    with stage(RETRIEVE, config=config) as observed:
        result: dict[str, Any]
        with operation("retrieval") as measured:
            from core import settings

            tenant_id = config.get("configurable", {}).get("tenant_id")
            document_ids = config.get("configurable", {}).get("rag_document_ids")
            if settings.TENANT_MODE and (not tenant_id or document_ids is None):
                result = {
                    "status": "error",
                    "error_code": "missing_tenant",
                    "message": "缺少可信团队上下文",
                    "hits": [],
                }
                measured["outcome"] = "error"
            else:
                result = await _measured_search(query, measured, tenant_id, document_ids)
        observed["outcome"] = measured["outcome"]
        observed["hits"] = len(result["hits"])
        observed["query_chars"] = len(query)
        if result.get("error_code"):
            observed["error_code"] = result["error_code"]
        if result["hits"]:
            observed["top_score"] = max(float(hit["score"]) for hit in result["hits"])
        return result


async def _measured_search(
    query: str, measured: dict, tenant_id: str | None = None, document_ids: list[str] | None = None
) -> dict:
    try:
        # 当前复用同步 SDK，因此移到工作线程，避免阻塞 FastAPI 的 asyncio 事件循环。
        # 外层超时后线程中的 HTTP 请求可能继续运行至自身超时；它们都是只读请求。
        async with asyncio.timeout(25):
            hits = (
                await asyncio.to_thread(
                    search_documents, query, tenant_id=tenant_id, document_ids=document_ids
                )
                if tenant_id
                else await asyncio.to_thread(search_documents, query)
            )
    except (RetrievalError, TimeoutError) as exc:
        measured["outcome"] = "timeout" if isinstance(exc, TimeoutError) else "error"
        code = exc.code if isinstance(exc, RetrievalError) else "timeout"
        message = str(exc) if isinstance(exc, RetrievalError) else "知识库检索超时，请稍后重试。"
        logger.warning("文档检索失败：%s", code)
        return {"status": "error", "error_code": code, "message": message, "hits": []}
    if not hits:
        measured["outcome"] = "no_results"
    return {
        "status": "ok" if hits else "no_results",
        "message": "返回相关片段，请判断适用性并引用来源。"
        if hits
        else "没有达到阈值的资料，不能据此推断知识库绝对没有答案。",
        "hits": [hit.model_dump() for hit in hits],
    }
