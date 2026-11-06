"""Publish an existing completed report without running evaluation again."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from evaluation import save_json, timestamp  # noqa: E402
from feishu_reports import FeishuReportPublisher  # noqa: E402
from run_feishu import FeishuHTTPTransport, FeishuSettings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="单独发布已有评测报告，并向指定 open_id 授予阅读权限"
    )
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--open-id", required=True)
    parser.add_argument(
        "--receipt", type=Path, help="另存发布记录；每次发布都创建新文档，不覆盖已有记录"
    )
    args = parser.parse_args()
    receipt_path = args.receipt or args.run_dir / "publication.json"
    try:
        summary = json.loads((args.run_dir / "summary.json").read_text(encoding="utf-8"))
        if summary.get("status") != "completed":
            raise ValueError("只发布已完成运行的报告")
        if receipt_path.exists():
            raise ValueError("已有发布记录，为避免重复创建文档，本命令不会覆盖或再次发布")
        settings = FeishuSettings()
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    publisher = FeishuReportPublisher(
        FeishuHTTPTransport(settings),
        settings.FEISHU_APP_ID,
        settings.FEISHU_APP_SECRET.get_secret_value(),
        settings.FEISHU_REPORT_FOLDER_TOKEN,
    )
    receipt = {"status": "publishing", "owner_open_id": args.open_id, "started_at": timestamp()}
    save_json(receipt_path, receipt)

    def created(document_id: str) -> None:
        receipt["document_id"] = document_id
        save_json(receipt_path, receipt)

    try:
        url = publisher.publish(
            args.run_dir / "report.md",
            f"SupportPilot 评测 {summary['evaluation_id'][:8]}",
            args.open_id,
            created,
        )
    except Exception as exc:
        receipt.update(
            status="failed", error=f"{type(exc).__name__}: {exc}", finished_at=timestamp()
        )
        save_json(receipt_path, receipt)
        parser.exit(1, f"报告发布失败，本地报告和发布记录已保留：{exc}\n")
    receipt.update(status="succeeded", report_url=url, finished_at=timestamp())
    save_json(receipt_path, receipt)
    print(f"报告已发布，已核对指定用户阅读权限：{url}")


if __name__ == "__main__":
    main()
