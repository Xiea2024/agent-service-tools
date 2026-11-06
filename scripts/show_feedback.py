"""Inspect persisted Feishu feedback locally without contacting the model or Feishu."""

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from feedback import FeedbackStore  # noqa: E402


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="查询本地飞书回答反馈")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(os.getenv("FEISHU_FEEDBACK_DB_PATH", ROOT / ".local/feishu-feedback.sqlite3")),
    )
    parser.add_argument("--app-id", default=os.getenv("FEISHU_APP_ID"))
    parser.add_argument("--snapshot-id", help="查看一条回答快照及其评价")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if not args.app_id or not args.db.is_file() or not 1 <= args.limit <= 100:
        parser.error("请检查 FEISHU_APP_ID、已存在的反馈数据库和 limit（1–100）")
    store = FeedbackStore(args.db, args.app_id, "dev-issue-agent")
    try:
        if args.snapshot_id:
            record = store.get_feedback(args.snapshot_id)
            output = {
                "answer": asdict(store.get_answer(args.snapshot_id)),
                "feedback": asdict(record) if record else None,
            }
        else:
            output = [asdict(record) for record in store.recent_feedback(args.limit)]
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
