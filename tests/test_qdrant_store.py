"""Qdrant 存储层离线集成测试：用官方 SDK 的本地模式验证真实写入与重开。

使用合成文档与假向量，不依赖 Docker 或网络；与真实服务器验证分开记录。
"""

import runpy
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from qdrant_client import QdrantClient, models
from test_document_vectors import make_batch, make_vectors

import qdrant_store
from qdrant_store import QdrantSettings, index_vectors


def test_upsert_payload_mapping_idempotence_and_persistence(tmp_path):
    data = make_vectors()
    path = str(tmp_path / "qdrant")
    with closing(QdrantClient(path=path)) as client:
        assert index_vectors(client, "docs", data) == len(data.vectors)
        assert index_vectors(client, "docs", data) == len(data.vectors)
        assert client.count("docs", exact=True).count == len(data.vectors)
        points = client.retrieve(
            "docs", ids=[c.chunk_id for c in data.batch.chunks], with_vectors=True
        )
        stored = {str(p.id): p for p in points}
        for chunk, vector in zip(data.batch.chunks, data.vectors, strict=True):
            point = stored[chunk.chunk_id]
            assert point.payload["content"] == chunk.content
            assert point.payload["source_url"] == chunk.source_url
            assert point.payload["chunk_index"] == chunk.chunk_index
            assert len(point.vector) == len(vector)
        # Cosine 会归一化存储向量，所以查询方向与片段 ID 才是这里要验证的关系。
        hits = client.query_points("docs", query=data.vectors[0], limit=1).points
        assert str(hits[0].id) == data.batch.chunks[0].chunk_id
    with closing(QdrantClient(path=path)) as reopened:
        assert reopened.count("docs", exact=True).count == len(data.vectors)
        assert reopened.get_collection("docs").config.params.vectors.size == 3


@pytest.mark.parametrize("kind", ["dimension", "model", "provider", "distance", "unmanaged"])
def test_incompatible_collection_refused_without_data_loss(kind):
    data = make_vectors()
    with closing(QdrantClient(":memory:")) as client:
        index_vectors(client, "docs", data)
        if kind == "dimension":
            data.vectors = [v + [1.0] for v in data.vectors]
        elif kind == "model":
            data.embedding_model = "another-model-with-the-same-dimension"
        elif kind == "provider":
            data.embedding_base_url = "https://another.example/v1"
        elif kind == "distance":
            client.create_collection(
                "other",
                vectors_config=models.VectorParams(size=3, distance=models.Distance.DOT),
                metadata=client.get_collection("docs").config.metadata,
            )
        else:
            client.create_collection(
                "other", vectors_config=models.VectorParams(size=3, distance=models.Distance.COSINE)
            )
        collection = "other" if kind in {"distance", "unmanaged"} else "docs"
        with pytest.raises(ValueError, match="不匹配"):
            index_vectors(client, collection, data)
        assert client.count("docs", exact=True).count == len(data.batch.chunks)


def test_changed_document_refused_without_changing_existing_points():
    original = make_vectors()
    with closing(QdrantClient(":memory:")) as client:
        index_vectors(client, "docs", original)
        changed = make_vectors(make_batch("更新后的说明。\n" * 15))
        with pytest.raises(ValueError, match="不同版本"):
            index_vectors(client, "docs", changed)
        assert client.count("docs", exact=True).count == len(original.vectors)


def test_mutated_invalid_vectors_do_not_touch_database():
    data = make_vectors()
    data.vectors.pop()
    client = Mock()
    with pytest.raises(ValidationError):
        index_vectors(client, "docs", data)
    client.collection_exists.assert_not_called()


def test_qdrant_settings_and_client_do_not_expose_key(monkeypatch):
    settings = QdrantSettings(_env_file=None, QDRANT_API_KEY="fake-qdrant-only")
    factory = Mock()
    monkeypatch.setattr(qdrant_store, "QdrantClient", factory)
    qdrant_store.create_qdrant_client(settings)
    assert "fake-qdrant-only" not in repr(settings)
    assert factory.call_args.kwargs["api_key"] == "fake-qdrant-only"
    assert factory.call_args.kwargs["timeout"] == 15


def test_cli_reuses_vectors_without_embedding(tmp_path, monkeypatch, capsys):
    data = make_vectors()
    path = tmp_path / "source.vectors.json"
    path.write_text(data.model_dump_json(), encoding="utf-8")
    client = QdrantClient(":memory:")
    monkeypatch.setattr(qdrant_store, "create_qdrant_client", lambda _: client)
    monkeypatch.setenv("QDRANT_COLLECTION", "cli-test")
    monkeypatch.setattr("sys.argv", ["index_document_chunks.py", str(path), "--reuse-vectors"])
    runpy.run_path(
        str(Path(__file__).parents[1] / "scripts/index_document_chunks.py"), run_name="__main__"
    )
    output = capsys.readouterr().out
    assert "不调用 Embedding" in output
    assert "读回验证成功" in output
    assert data.batch.chunks[0].content not in output
