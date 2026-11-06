"""单篇飞书文档读取入口：接收 wiki/docx 链接，展示预览并按需保存 JSON。

从任意工作目录运行时均能定位项目源码和根目录 .env；不启动机器人或调用 LLM。
保存文件的路径由调用者指定，建议使用已加入 Git 忽略规则的 .local/feishu-documents/。
"""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pydantic import ValidationError  # noqa: E402

from feishu_documents import FeishuDocumentError, FeishuDocumentReader  # noqa: E402
from run_feishu import FeishuSettings  # noqa: E402


def main() -> None:
    """链接通过参数提供，凭据仅从本机配置读取；默认只展示前 500 个字符。"""
    parser = argparse.ArgumentParser(description="读取已授权的单篇飞书新版文档")
    parser.add_argument("url", help="具体文档的 wiki/docx 链接")
    parser.add_argument("--output", type=Path, help="将完整正文和来源保存为 UTF-8 JSON")
    parser.add_argument(
        "--preview-chars", type=int, default=500, help="预览字符数，0 表示只输出元信息"
    )
    args = parser.parse_args()
    if args.preview_chars < 0:
        parser.error("--preview-chars 不能为负数")
    try:
        settings = FeishuSettings()
    except ValidationError as exc:
        fields = ", ".join(str(error["loc"][0]) for error in exc.errors(include_input=False))
        parser.exit(1, f"请检查根目录 .env 配置：{fields}\n")
    try:
        document = FeishuDocumentReader(settings).read(args.url)
    except (FeishuDocumentError, ValueError) as exc:
        parser.exit(1, f"读取失败：{exc}\n")
    print(f"标题：{document.title}\n来源：{document.source_url}")
    print(
        f"文档 ID：{document.document_id}\n版本：{document.revision_id}\n正文字符数：{len(document.content)}"
    )
    if args.preview_chars:
        print("正文预览：\n" + document.content[: args.preview_chars])
    if not document.content.strip():
        print("提示：正文为空，请在飞书页面中补充资料。")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(asdict(document), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"完整内容已保存：{args.output.resolve()}")


if __name__ == "__main__":
    main()
