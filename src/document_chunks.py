"""RAG 数据准备层：将一篇已读取的文档切成可追溯的小片段。

输入为飞书读取脚本导出的文档结构，输出包含正文、稳定编号和来源的片段。
本模块仅在内存中切分，不访问飞书、Embedding API 或向量数据库。
"""

import json
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from langchain_text_splitters import RecursiveCharacterTextSplitter
from pydantic import BaseModel, ConfigDict, Field

CHUNKING_STRATEGY = "recursive-character-v1"


class SourceDocument(BaseModel):
    """校验本地 JSON 的输入结构，避免缺失来源后仍然生成无法引用的片段。

    与 read_feishu_document.py 的导出字段保持一致。strict=True 防止把错误类型
    静默转换成文本；extra='forbid' 可以发现误把 chunks 文件当作文档输入的情况。
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    document_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    content: str
    revision_id: int | None = None


@dataclass(frozen=True)
class DocumentChunk:
    """一个可检索片段；frozen=True 防止创建后意外改动编号和内容的对应关系。"""

    document_id: str
    chunk_id: str
    chunk_index: int  # 从 0 开始，表示它在这份文档中的先后顺序。
    title: str
    source_url: str
    revision_id: int | None
    content: str
    start_char: int  # 在原文中的起点，包含这个位置。
    end_char: int  # 在原文中的终点，不包含这个位置：原文[start_char:end_char]。


def chunk_document(
    document: SourceDocument, *, chunk_size: int = 500, chunk_overlap: int = 80
) -> list[DocumentChunk]:
    """优先按段落/行切分，过长时继续按中文标点、空格、字符递归拆分。

    chunk_size 是单片段的字符长度上限，chunk_overlap 是合并片段时的目标重叠。
    按语义边界切分时不保证恰好重叠这么多字符，跨递归分支时可能没有重叠。
    保留原文空白，再用起止位置检查内容覆盖，避免漏掉换行、缩进或整段内容。
    """
    if chunk_size <= 0 or not 0 <= chunk_overlap < chunk_size:
        raise ValueError("chunk_size 必须大于 0，chunk_overlap 必须满足 0 <= overlap < size")
    if not document.content.strip():
        # 空文档/纯空白文档没有可检索的内容，不制造一个空片段发送给 Embedding。
        return []

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        length_function=len,  # 此处按 Python 字符数计长，不能当作模型 Token 数。
        separators=["\n\n", "\n", "。", "！", "？", "；", " ", ""],
        keep_separator="end",  # 将换行/标点留在前一段末尾，避免分隔符丢失。
        strip_whitespace=False,  # 保留代码缩进和首尾换行，不在本轮做有损清洗。
        add_start_index=True,  # LangChain 将片段在原文的起点写入 metadata。
    )
    pieces = splitter.create_documents([document.content])
    chunks: list[DocumentChunk] = []
    covered_until = 0
    for index, piece in enumerate(pieces):
        start = piece.metadata["start_index"]
        end = start + len(piece.page_content)
        # 连续换行等短片段可能被库定位到上一段已包含的相同文本。
        # 重新寻找能延伸覆盖范围的出现位置，避免把两个不同换行当成同一个。
        if start >= 0 and end <= covered_until:
            start = document.content.find(
                piece.page_content, max(0, covered_until - len(piece.page_content) + 1)
            )
            end = start + len(piece.page_content)
        # 相邻片段允许重叠，但不允许留空档；每个片段还必须能匹配到原文。
        if start < 0 or start > covered_until or document.content[start:end] != piece.page_content:
            raise ValueError("切分结果无法连续覆盖原文，请检查片段定位或切分参数")
        covered_until = max(covered_until, end)

        # UUID5 对相同输入总是生成相同 ID；加入 index 可区分内容完全相同的不同片段。
        # 内容、位置顺序或参数变化时，ID 可能变化；这不是自动删除旧索引的机制。
        identity = json.dumps(
            [
                CHUNKING_STRATEGY,
                document.document_id,
                chunk_size,
                chunk_overlap,
                index,
                piece.page_content,
            ],
            ensure_ascii=False,
        )
        chunks.append(
            DocumentChunk(
                document_id=document.document_id,
                chunk_id=str(uuid5(NAMESPACE_URL, identity)),
                chunk_index=index,
                title=document.title,
                source_url=document.source_url,
                revision_id=document.revision_id,
                content=piece.page_content,
                start_char=start,
                end_char=end,
            )
        )
    if covered_until != len(document.content):
        raise ValueError("切分结果没有覆盖原文末尾，未导出不完整结果")
    return chunks
