"""Real local Qdrant: the same document cannot overwrite or leak across teams."""

from contextlib import closing
from unittest.mock import Mock
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient
from test_document_retrieval import configs
from test_document_vectors import make_vectors

from document_retrieval import query_index, search_feishu_documents
from qdrant_store import index_vectors


def test_shared_collection_separates_same_document_and_legacy_points():
    a, b = str(uuid4()), str(uuid4())
    data = make_vectors()
    embeddings = Mock()
    embeddings.embed_query.return_value = data.vectors[0]
    with closing(QdrantClient(":memory:")) as client:
        index_vectors(client, "docs", data)
        index_vectors(client, "docs", data, tenant_id=a)
        index_vectors(client, "docs", data, tenant_id=b)
        index_vectors(client, "docs", data, tenant_id=a)
        assert client.count("docs").count == 3 * len(data.vectors)
        args = ("文档", client, embeddings, *configs())
        ah = query_index(*args, tenant_id=a)
        bh = query_index(*args, tenant_id=b)
        legacy = query_index(*args)
        assert ah and bh and legacy
        assert {h.chunk_id for h in ah}.isdisjoint(h.chunk_id for h in bh)
        assert {h.chunk_id for h in ah}.isdisjoint(h.chunk_id for h in legacy)
        assert query_index(*args, tenant_id=str(uuid4())) == []


@pytest.mark.asyncio
async def test_tool_gets_tenant_from_config_not_model_arguments(monkeypatch):
    from core import settings

    monkeypatch.setattr(settings, "TENANT_MODE", True)
    search = Mock(return_value=[])
    monkeypatch.setattr("document_retrieval.search_documents", search)
    tenant = str(uuid4())
    await search_feishu_documents.ainvoke(
        {"query": "问题"},
        config={"configurable": {"tenant_id": tenant, "rag_document_ids": ["doc"]}},
    )
    search.assert_called_once_with("问题", tenant_id=tenant, document_ids=["doc"])
    search.reset_mock()
    result = await search_feishu_documents.ainvoke({"query": "问题"})
    assert result["error_code"] == "missing_tenant"
    search.assert_not_called()
    assert "tenant_id" not in search_feishu_documents.args
