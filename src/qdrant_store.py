"""RAG 存储层：校验 Collection 配置，将片段、向量与出处共同写入 Qdrant。

这里只处理初次入库与相同输入的重复导入。文档变更时明确拒绝混写旧片段，
等待后续实现更新/删除同步；不会为了维度冲突而删除用户已有的 Collection。
"""

from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from qdrant_client import QdrantClient, models

from document_vectors import DocumentVectors


class QdrantSettings(BaseSettings):
    """与聊天配置分开；默认连接本机 Docker 的 REST 端口。"""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent / ".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )
    QDRANT_URL: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:6333")
    QDRANT_COLLECTION: str = Field(default="dev_issue_documents_bge_m3_v1", min_length=1)
    QDRANT_API_KEY: SecretStr | None = None
    QDRANT_TIMEOUT: int = Field(default=15, gt=0, le=120)


def create_qdrant_client(settings: QdrantSettings) -> QdrantClient:
    """由调用方负责 close；认证仅用于请求，不进入 payload 或向量文件。"""
    return QdrantClient(
        url=str(settings.QDRANT_URL).rstrip("/"),
        api_key=settings.QDRANT_API_KEY.get_secret_value() if settings.QDRANT_API_KEY else None,
        timeout=settings.QDRANT_TIMEOUT,
    )


def index_vectors(
    client: QdrantClient, collection: str, data: DocumentVectors, *, tenant_id: str | None = None
) -> int:
    """先校验再 upsert，最后逐批读回核对；返回本次确认写入的片段数。"""
    # 即使调用方在创建模型后修改了内部 list，入库前也必须重新校验。
    data = DocumentVectors.model_validate_json(data.model_dump_json())
    if tenant_id:
        tenant_id = str(UUID(tenant_id))
    profile = {
        "index_schema": "dev-issue-documents-v1",
        "embedding_model": data.embedding_model,
        "embedding_base_url": data.embedding_base_url,
    }
    if not client.collection_exists(collection):
        client.create_collection(
            collection_name=collection,
            vectors_config=models.VectorParams(
                size=data.dimension, distance=models.Distance.COSINE
            ),
            metadata=profile,
        )
    info = client.get_collection(collection)
    params = info.config.params.vectors
    if (
        not isinstance(params, models.VectorParams)
        or params.size != data.dimension
        or params.distance != models.Distance.COSINE
        or info.config.metadata != profile
    ):
        raise ValueError("Collection 的维度、距离或 Embedding 配置不匹配，请使用新的 Collection")

    # 指纹包含正文 hash、切分参数和全部元信息。相同文档的不同版本暂不混写，
    # 因为 upsert 只覆盖相同 ID，不会自动删除旧版本中已经消失的片段。
    fingerprint = sha256(data.batch.model_dump_json().encode("utf-8")).hexdigest()
    document_filter = models.FieldCondition(
        key="document_id", match=models.MatchValue(value=data.batch.document.document_id)
    )
    stale_filter = models.Filter(
        must=[
            document_filter,
            models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id))
            if tenant_id
            else models.IsEmptyCondition(is_empty=models.PayloadField(key="tenant_id")),
        ],
        must_not=[
            models.FieldCondition(
                key="import_fingerprint", match=models.MatchValue(value=fingerprint)
            )
        ],
    )
    if client.count(collection, count_filter=stale_filter, exact=True).count:
        raise ValueError("该文档已有不同版本或切分参数；更新同步尚未实现，请使用新的 Collection")

    confirmed = 0
    for start in range(0, len(data.vectors), 64):
        # 不将正文与向量分开排序或分开写库：同一个 Point 包含 ID + vector + payload。
        points = [
            models.PointStruct(
                id=str(uuid5(NAMESPACE_URL, f"supportpilot:{tenant_id}:{chunk.chunk_id}"))
                if tenant_id
                else chunk.chunk_id,
                vector=vector,
                payload={
                    **asdict(chunk),
                    **profile,
                    "import_fingerprint": fingerprint,
                    "content_sha256": data.batch.document.content_sha256,
                    "chunking": data.batch.chunking.model_dump(),
                    **({"tenant_id": tenant_id} if tenant_id else {}),
                },
            )
            for chunk, vector in zip(
                data.batch.chunks[start : start + 64], data.vectors[start : start + 64], strict=True
            )
        ]
        client.upsert(collection, points=points, wait=True)
        stored = client.retrieve(collection, ids=[p.id for p in points], with_payload=True)
        expected = {p.id: p.payload for p in points}
        if len(stored) != len(points) or any(expected.get(p.id) != p.payload for p in stored):
            raise RuntimeError("写入后读回的片段或出处不一致，请检查 Qdrant；可用同一向量文件重试")
        confirmed += len(stored)
    return confirmed
