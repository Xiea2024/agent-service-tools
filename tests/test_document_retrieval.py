"""文档检索离线测试：验证模型一致性、真实向量查询、阈值与错误反馈。

使用官方 Qdrant 本地模式和合成数据；Embedding 为假向量，不访问真实 API。
工具错误不得把密钥或 SDK 原始响应传给聊天模型。
"""

import runpy
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from openai import APIConnectionError
from pydantic import ValidationError
from qdrant_client import QdrantClient
from test_document_vectors import make_vectors

import document_retrieval
from document_retrieval import (
    RetrievalError,
    RetrievalSettings,
    SearchHit,
    query_index,
    search_feishu_documents,
)
from embedding_client import EmbeddingSettings
from qdrant_store import QdrantSettings, index_vectors


def configs(**overrides):
    """明确禁用本机 .env，确保测试只使用合成配置。"""
    return (
        EmbeddingSettings(
            _env_file=None,
            EMBEDDING_API_KEY="fake-only",
            EMBEDDING_MODEL="test-model",
            EMBEDDING_BASE_URL="https://example.com/v1",
        ),
        QdrantSettings(_env_file=None, QDRANT_COLLECTION="docs"),
        RetrievalSettings(_env_file=None, **overrides),
    )


def test_query_returns_corresponding_payload_and_limits_results():
    data = make_vectors()
    embeddings = Mock()
    embeddings.embed_query.return_value = data.vectors[0]
    with closing(QdrantClient(":memory:")) as client:
        index_vectors(client, "docs", data)
        hits = query_index("  接口报错  ", client, embeddings, *configs(RAG_TOP_K=1))
    embeddings.embed_query.assert_called_once_with("接口报错")
    assert len(hits) == 1
    assert hits[0].chunk_id == data.batch.chunks[0].chunk_id
    assert hits[0].content == data.batch.chunks[0].content
    assert hits[0].source_url == data.batch.chunks[0].source_url
    assert hits[0].score == pytest.approx(1, abs=1e-5)


def test_low_score_is_no_match_instead_of_unrelated_evidence():
    data = make_vectors()
    embeddings = Mock()
    embeddings.embed_query.return_value = [-1.0, -1.0, 0.0]
    with closing(QdrantClient(":memory:")) as client:
        index_vectors(client, "docs", data)
        assert query_index("无关问题", client, embeddings, *configs()) == []


@pytest.mark.parametrize("field", ["EMBEDDING_MODEL", "EMBEDDING_BASE_URL"])
def test_model_identity_checked_before_embedding(field):
    data = make_vectors()
    embedding_config, qdrant_config, retrieval_config = configs()
    if field == "EMBEDDING_MODEL":
        embedding_config.EMBEDDING_MODEL = "different-model"
    else:
        from pydantic import AnyHttpUrl

        embedding_config.EMBEDDING_BASE_URL = AnyHttpUrl("https://other.example/v1")
    embeddings = Mock()
    with closing(QdrantClient(":memory:")) as client:
        index_vectors(client, "docs", data)
        with pytest.raises(RetrievalError, match="不匹配"):
            query_index(
                "配置", client, embeddings, embedding_config, qdrant_config, retrieval_config
            )
    embeddings.embed_query.assert_not_called()


@pytest.mark.parametrize("vector", [[1.0], [0.0, 0.0, 0.0], [float("nan"), 1.0, 1.0]])
def test_invalid_query_vector_rejected(vector):
    embeddings = Mock()
    embeddings.embed_query.return_value = vector
    with closing(QdrantClient(":memory:")) as client:
        index_vectors(client, "docs", make_vectors())
        with pytest.raises(RetrievalError, match="查询向量"):
            query_index("配置", client, embeddings, *configs())


def test_hit_text_is_bounded_and_truncation_is_explicit():
    data = make_vectors()
    embeddings = Mock()
    embeddings.embed_query.return_value = data.vectors[0]
    with closing(QdrantClient(":memory:")) as client:
        index_vectors(client, "docs", data)
        client.set_payload(
            "docs", payload={"content": "合成正文" * 100}, points=[data.batch.chunks[0].chunk_id]
        )
        hits = query_index("配置", client, embeddings, *configs(RAG_TOP_K=1, RAG_MAX_CHARS=100))
    assert len(hits[0].content) == 100
    assert hits[0].truncated


@pytest.mark.parametrize("query", ["", " " * 2, "问" * 1001])
def test_invalid_question_never_reaches_embedding(query):
    embeddings, client = Mock(), Mock()
    with pytest.raises((ValidationError, RetrievalError)):
        query_index(query, client, embeddings, *configs())
    embeddings.embed_query.assert_not_called()
    client.get_collection.assert_not_called()


@pytest.mark.asyncio
async def test_tool_distinguishes_success_no_results_and_errors(monkeypatch):
    hit = SearchHit(
        chunk_id="point",
        document_id="doc",
        title="合成资料",
        source_url="https://example.com/doc",
        content="合成正文",
        score=0.8,
    )
    search = Mock(return_value=[hit])
    monkeypatch.setattr(document_retrieval, "search_documents", search)
    success = await search_feishu_documents.ainvoke({"query": "接口错误"})
    assert success["status"] == "ok"
    assert success["hits"][0]["source_url"] == hit.source_url
    search.return_value = []
    assert (await search_feishu_documents.ainvoke({"query": "其他问题"}))["status"] == "no_results"
    search.side_effect = RetrievalError("qdrant_unavailable", "知识库不可用。")
    failed = await search_feishu_documents.ainvoke({"query": "接口错误"})
    assert failed["status"] == "error"
    assert failed["error_code"] == "qdrant_unavailable"
    assert failed["hits"] == []


@pytest.mark.asyncio
async def test_timeout_has_explicit_error_status(monkeypatch):
    monkeypatch.setattr(document_retrieval, "search_documents", Mock(side_effect=TimeoutError()))
    result = await search_feishu_documents.ainvoke({"query": "配置"})
    assert result["status"] == "error"
    assert result["error_code"] == "timeout"


@pytest.mark.asyncio
async def test_provider_error_does_not_expose_original_response(monkeypatch):
    import qdrant_store

    embedding_config, qdrant_config, retrieval_config = configs()
    client = QdrantClient(":memory:")
    index_vectors(client, "docs", make_vectors())
    embeddings = Mock()
    embeddings.embed_query.side_effect = APIConnectionError(
        message="fake-private-provider-response",
        request=httpx.Request("POST", "https://example.com/v1/embeddings"),
    )
    monkeypatch.setattr(document_retrieval, "EmbeddingSettings", lambda: embedding_config)
    monkeypatch.setattr(document_retrieval, "RetrievalSettings", lambda: retrieval_config)
    monkeypatch.setattr(document_retrieval, "create_embeddings", lambda *args, **kwargs: embeddings)
    monkeypatch.setattr(qdrant_store, "QdrantSettings", lambda: qdrant_config)
    monkeypatch.setattr(qdrant_store, "create_qdrant_client", lambda _: client)
    result = await search_feishu_documents.ainvoke({"query": "配置"})
    assert result["status"] == "error"
    assert result["error_code"] == "embedding_unavailable"
    assert "fake-private-provider-response" not in str(result)


def test_query_cli_uses_shared_retrieval_entry(monkeypatch, capsys):
    hit = SearchHit(
        chunk_id="point",
        document_id="doc",
        title="合成资料",
        source_url="https://example.com/doc",
        content="合成正文",
        score=0.8,
    )
    search = Mock(return_value=[hit])
    monkeypatch.setattr(document_retrieval, "search_documents", search)
    monkeypatch.setattr("sys.argv", ["query_documents.py", "接口问题", "--preview-chars", "0"])
    runpy.run_path(
        str(Path(__file__).parents[1] / "scripts/query_documents.py"), run_name="__main__"
    )
    output = capsys.readouterr().out
    assert "https://example.com/doc" in output
    assert hit.content not in output
    search.assert_called_once_with("接口问题")
