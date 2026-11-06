"""本地文档切分入口：读取单篇飞书文档 JSON，导出带出处的 chunks JSON。

文件读写由本脚本负责，切分规则在 document_chunks.py，便于分别阅读和测试。
本轮不调用外部服务；输出继续放在已被 Git 忽略的本地文档目录中。
"""

import argparse
import json
import sys
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

# 命令行直接运行 scripts/ 下的文件时，Python 不会自动把项目 src 加入导入路径。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pydantic import ValidationError  # noqa: E402

from document_chunks import CHUNKING_STRATEGY, SourceDocument, chunk_document  # noqa: E402


def main() -> None:
    """先校验输入，再切分与保存；任何失败都以非零退出码反馈。"""
    parser = argparse.ArgumentParser(description="将本地飞书正文切分为保留出处的片段")
    parser.add_argument("input", type=Path, help="read_feishu_document.py 导出的 JSON 文件")
    parser.add_argument("--output", type=Path, help="默认保存为输入文件同目录的 名称.chunks.json")
    parser.add_argument("--chunk-size", type=int, default=500, help="片段字符长度上限，默认 500")
    parser.add_argument("--chunk-overlap", type=int, default=80, help="目标重叠字符数，默认 80")
    parser.add_argument(
        "--preview-chars", type=int, default=120, help="每段预览字符数，0 表示不展示正文"
    )
    args = parser.parse_args()
    if args.preview_chars < 0:
        parser.error("--preview-chars 不能为负数")
    output = args.output or args.input.with_name(args.input.stem + ".chunks.json")
    if output.resolve() == args.input.resolve():
        parser.error("输出路径不能与输入相同，避免覆盖原始文档")

    try:
        # model_validate_json 同时检查 JSON 语法与字段结构，密钥不参与本轮处理。
        document = SourceDocument.model_validate_json(args.input.read_text(encoding="utf-8"))
        chunks = chunk_document(
            document, chunk_size=args.chunk_size, chunk_overlap=args.chunk_overlap
        )
    except ValidationError as exc:
        # 只说明出错字段与原因，避免把用户整篇正文放进错误堆栈。
        errors = "; ".join(
            f"{'.'.join(map(str, error['loc'])) or 'JSON'}: {error['msg']}"
            for error in exc.errors(include_input=False)
        )
        parser.exit(1, f"文档格式不合法：{errors}\n")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"读取或切分失败：{exc}\n")

    # 导出参数和原文 hash，方便以后知道这些片段由哪份内容、哪套规则生成。
    # 保留确定性的内容，不写当前时间，确保相同输入和参数可以重现同一份结果。
    payload = {
        "schema_version": 1,
        "document": {
            **document.model_dump(exclude={"content"}),
            "content_sha256": sha256(document.content.encode("utf-8")).hexdigest(),
            "character_count": len(document.content),
        },
        "chunking": {
            "strategy": CHUNKING_STRATEGY,
            "chunk_size": args.chunk_size,
            "chunk_overlap": args.chunk_overlap,
            "length_unit": "characters",
        },
        "chunks": [asdict(chunk) for chunk in chunks],
    }
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        parser.exit(1, f"保存片段失败：{exc}\n")
    print(f"文档：{document.title}\n原文字符数：{len(document.content)}\n片段数：{len(chunks)}")
    for chunk in chunks:
        print(
            f"片段 {chunk.chunk_index}：{len(chunk.content)} 字符，"
            f"原文范围 [{chunk.start_char}, {chunk.end_char})，ID={chunk.chunk_id}"
        )
        if args.preview_chars:
            print(chunk.content[: args.preview_chars])
    if not chunks:
        print("正文为空或仅有空白，本次导出空片段列表。")
    print(f"切分结果已保存：{output.resolve()}")


if __name__ == "__main__":
    main()
