"""Inspect a persisted evaluation task and its local report location."""

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from evaluation_jobs import EvaluationJobStore  # noqa: E402


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="查询本地评测任务状态和报告位置")
    parser.add_argument("job_id")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(os.getenv("FEISHU_EVAL_DB_PATH", ROOT / ".local/evaluation-jobs.sqlite3")),
    )
    parser.add_argument("--app-id", default=os.getenv("FEISHU_APP_ID"))
    args = parser.parse_args()
    if not args.app_id or not args.db.is_file():
        parser.error("请检查应用 ID 和已存在的任务数据库")
    try:
        job = EvaluationJobStore(args.db, args.app_id, "dev-issue-agent").get(args.job_id)
    except ValueError as exc:
        parser.error(str(exc))
    output: dict[str, Any] = asdict(job)
    output.pop("suite_json")
    output.pop("summary_json")
    output["summary"] = job.summary
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
