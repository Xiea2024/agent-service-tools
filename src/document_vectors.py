"""RAG 数据准备层：校验 chunks 文件，并生成与片段逐一对应的文档向量。

复用已有 Embedding 客户端；本模块不连接 Qdrant。导出的向量文件保留模型、
来源和切分参数，入库失败后可以重用，避免为了重试存储而再次付费向量化。
"""

from hashlib import sha256
from typing import Literal

from langchain_core.embeddings import Embeddings
from pydantic import BaseModel, ConfigDict, Field, model_validator

from document_chunks import DocumentChunk, SourceDocument, chunk_document
from embedding_client import EmbeddingSettings


class DocumentMetadata(BaseModel):
    """与切分脚本的 document 字段一致，不重复保存整篇正文。"""

    model_config = ConfigDict(strict=True, extra="forbid")
    document_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    revision_id: int | None = None
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    character_count: int = Field(gt=0)


class ChunkingConfig(BaseModel):
    """固定当前支持的切分协议，避免把其他格式误当作已验证的输入。"""

    model_config = ConfigDict(strict=True, extra="forbid")
    strategy: Literal["recursive-character-v1"]
    chunk_size: int = Field(gt=0)
    chunk_overlap: int = Field(ge=0)
    length_unit: Literal["characters"]


class ChunkBatch(BaseModel):
    """一篇完整文档的片段清单；空正文应停在切分阶段，不调用 Embedding。"""

    model_config = ConfigDict(strict=True, extra="forbid")
    schema_version: Literal[1]
    document: DocumentMetadata
    chunking: ChunkingConfig
    chunks: list[DocumentChunk] = Field(min_length=1)

    @model_validator(mode="after")
    def check_source(self) -> "ChunkBatch":
        """拼回正文并重新核对切分，防止缺片段、乱序、错 ID 或错出处被入库。"""
        text = ""
        for chunk in self.chunks:
            start, end = chunk.start_char, chunk.end_char
            if start < 0 or start > len(text) or end <= len(text):
                raise ValueError("片段范围不能有空缺、倒序或重复覆盖")
            overlap = len(text) - start
            if text[start:] != chunk.content[:overlap]:
                raise ValueError("相邻片段的重叠正文不一致")
            text += chunk.content[overlap:]
        if (
            len(text) != self.document.character_count
            or sha256(text.encode("utf-8")).hexdigest() != self.document.content_sha256
        ):
            raise ValueError("片段无法还原为原文，请重新运行切分脚本")
        source = SourceDocument(
            **self.document.model_dump(exclude={"content_sha256", "character_count"}), content=text
        )
        expected = chunk_document(
            source,
            chunk_size=self.chunking.chunk_size,
            chunk_overlap=self.chunking.chunk_overlap,
        )
        if self.chunks != expected:
            raise ValueError("片段编号、顺序、范围或来源与切分规则不一致")
        return self


class DocumentVectors(BaseModel):
    """vectors[i] 只能对应 batch.chunks[i]，两者一起保存和读取。"""

    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)
    schema_version: Literal[1] = 1
    batch: ChunkBatch
    embedding_model: str = Field(min_length=1)
    embedding_base_url: str = Field(min_length=1)
    vectors: list[list[float]]

    @model_validator(mode="after")
    def check_vectors(self) -> "DocumentVectors":
        """在创建 Collection 前一次检查全部结果，拒绝半份或不合法的向量。"""
        if len(self.vectors) != len(self.batch.chunks):
            raise ValueError("向量数必须等于片段数，不能静默截断或错位")
        size = len(self.vectors[0])
        if not size or any(len(v) != size or not any(v) for v in self.vectors):
            raise ValueError("向量必须非空、维度一致，且不能是 Cosine 无法使用的零向量")
        return self

    @property
    def dimension(self) -> int:
        """维度取自真实结果，不能把某个模型的 1024 硬编码为所有模型的维度。"""
        return len(self.vectors[0])


def embed_chunks(
    batch: ChunkBatch, embeddings: Embeddings, settings: EmbeddingSettings, *, batch_size: int = 16
) -> DocumentVectors:
    """按原顺序、小批量请求；先检查每批数量，再拼接，避免缺结果造成后续错位。"""
    if not 1 <= batch_size <= 32:
        raise ValueError("Embedding 每批片段数应在 1–32 之间")
    vectors: list[list[float]] = []
    for start in range(0, len(batch.chunks), batch_size):
        texts = [chunk.content for chunk in batch.chunks[start : start + batch_size]]
        result = embeddings.embed_documents(texts)
        if len(result) != len(texts):
            raise ValueError(f"第 {start // batch_size + 1} 批向量数与输入数不一致")
        vectors.extend(result)
    return DocumentVectors(
        batch=batch,
        embedding_model=settings.EMBEDDING_MODEL,
        embedding_base_url=str(settings.EMBEDDING_BASE_URL).rstrip("/"),
        vectors=vectors,
    )
