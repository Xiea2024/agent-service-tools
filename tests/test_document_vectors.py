"""文档向量化的离线测试：使用合成正文和假向量检查对应关系与失败行为。

不读取本机文档或密钥，也不调用硅基流动；假向量不代表真实模型的检索效果。
"""

import json
from dataclasses import asdict
from hashlib import sha256
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from document_chunks import CHUNKING_STRATEGY, SourceDocument, chunk_document
from document_vectors import ChunkBatch, DocumentVectors, embed_chunks
from embedding_client import EmbeddingSettings


def make_batch(content="接口失败，请检查配置。\n" * 8):
    """使用真正的切分函数生成合成输入，测试与生产脚本采用同一协议。"""
    document = SourceDocument(
        document_id="synthetic-doc",
        title="合成故障说明",
        source_url="https://example.com/docx/synthetic",
        revision_id=1,
        content=content,
    )
    payload = {
        "schema_version": 1,
        "document": {
            **document.model_dump(exclude={"content"}),
            "content_sha256": sha256(content.encode()).hexdigest(),
            "character_count": len(content),
        },
        "chunking": {
            "strategy": CHUNKING_STRATEGY,
            "chunk_size": 40,
            "chunk_overlap": 5,
            "length_unit": "characters",
        },
        "chunks": [asdict(c) for c in chunk_document(document, chunk_size=40, chunk_overlap=5)],
    }
    return ChunkBatch.model_validate_json(json.dumps(payload))


def make_vectors(batch=None):
    batch = batch or make_batch()
    return DocumentVectors(
        batch=batch,
        embedding_model="test-model",
        embedding_base_url="https://example.com/v1",
        vectors=[[float(i + 1), 1.0, 0.0] for i in range(len(batch.chunks))],
    )


def test_batch_order_and_vector_file_roundtrip():
    batch = make_batch()
    embeddings = Mock()
    embeddings.embed_documents.side_effect = lambda texts: [
        [float(len(text)), 1.0] for text in texts
    ]
    settings = EmbeddingSettings(_env_file=None, EMBEDDING_API_KEY="fake-only")
    data = embed_chunks(batch, embeddings, settings, batch_size=2)
    submitted = [
        text for call in embeddings.embed_documents.call_args_list for text in call.args[0]
    ]
    assert submitted == [c.content for c in batch.chunks]
    assert data.vectors == [[float(len(c.content)), 1.0] for c in batch.chunks]
    assert all(len(call.args[0]) <= 2 for call in embeddings.embed_documents.call_args_list)
    assert data.dimension == 2
    assert DocumentVectors.model_validate_json(data.model_dump_json()) == data
    assert "fake-only" not in data.model_dump_json()


def test_missing_batch_result_stops_before_next_batch():
    embeddings = Mock()
    embeddings.embed_documents.return_value = [[1.0, 2.0]]
    with pytest.raises(ValueError, match="第 1 批"):
        embed_chunks(
            make_batch(),
            embeddings,
            EmbeddingSettings(_env_file=None, EMBEDDING_API_KEY="fake-only"),
            batch_size=2,
        )
    embeddings.embed_documents.assert_called_once()


@pytest.mark.parametrize("kind", ["missing", "dimension", "empty", "zero", "nan", "inf"])
def test_invalid_vectors_are_rejected(kind):
    data = make_vectors().model_dump()
    if kind == "missing":
        data["vectors"].pop()
    else:
        data["vectors"][0] = {
            "dimension": [1.0],
            "empty": [],
            "zero": [0.0, 0.0, 0.0],
            "nan": [float("nan"), 1.0, 2.0],
            "inf": [float("inf"), 1.0, 2.0],
        }[kind]
    with pytest.raises(ValidationError):
        DocumentVectors.model_validate(data)


@pytest.mark.parametrize("kind", ["empty", "order", "content", "id", "source", "hash", "range"])
def test_invalid_manifest_rejected_before_embedding(kind):
    payload = json.loads(make_batch().model_dump_json())
    if kind == "empty":
        payload["chunks"] = []
    elif kind == "order":
        payload["chunks"].reverse()
    elif kind == "hash":
        payload["document"]["content_sha256"] = "0" * 64
    else:
        key = {"content": "content", "id": "chunk_id", "source": "source_url", "range": "end_char"}[
            kind
        ]
        payload["chunks"][0][key] = 999 if kind == "range" else "wrong-value"
    with pytest.raises(ValidationError):
        ChunkBatch.model_validate_json(json.dumps(payload))
