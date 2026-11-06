"""独立检索 CLI：输入问题，查看 Qdrant 命中片段及出处，便于定位 RAG 问题。

仅调用查询 Embedding 和向量数据库，不启动聊天模型、Agent 或飞书连接。
使用与 Agent 相同的检索入口，避免维护两套检索逻辑。
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from document_retrieval import RetrievalError, search_documents  # noqa: E402


def main() -> None:
    """默认展示正文，适合本机调试；--preview-chars 0 可只显示元信息。"""
    parser = argparse.ArgumentParser(description="用自然语言检索已入库的飞书文档")
    parser.add_argument("query", help="检索问题，最多 1000 字符")
    parser.add_argument("--preview-chars", type=int, default=500, help="每段正文预览长度，默认 500")
    args = parser.parse_args()
    if not args.query.strip() or len(args.query) > 1000 or args.preview_chars < 0:
        parser.error("问题应为 1–1000 个非纯空白字符，预览长度不能为负数")
    try:
        hits = search_documents(args.query)
    except RetrievalError as exc:
        parser.exit(1, f"检索失败 [{exc.code}]：{exc}\n")
    print(f"命中片段数：{len(hits)}")
    for index, hit in enumerate(hits, start=1):
        print(f"{index}. {hit.title} | 相似度={hit.score:.4f} | ID={hit.chunk_id}")
        print(f"来源：{hit.source_url}")
        if args.preview_chars:
            print(hit.content[: args.preview_chars])
    if not hits:
        print("没有达到当前相似度阈值的片段，请调整问题或补充资料。")


if __name__ == "__main__":
    main()
