"""Upgrade the task ledger after stopping Feishu; optionally create a consistent backup."""

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from evaluation_jobs import WorkerLock  # noqa: E402
from task_audit.migrations import SCHEMA_VERSION, migrate_task_database  # noqa: E402


def main(argv=None):
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="升级已有飞书任务数据库，不创建空数据库")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(os.getenv("FEISHU_TASK_DB_PATH", str(ROOT / ".local/feishu-tasks.sqlite3"))),
    )
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args(argv)
    if not args.db.is_file():
        parser.error("任务数据库不存在，请检查路径")
    lock = WorkerLock(args.db.with_suffix(".worker.lock"))
    lock.acquire()
    backup_path = None
    try:
        if args.backup_dir:
            args.backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = (
                args.backup_dir
                / f"tasks-{datetime.now(UTC):%Y%m%d-%H%M%S}-{uuid4().hex[:8]}.sqlite3"
            )
            with closing(
                sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)
            ) as source:
                with closing(sqlite3.connect(backup_path)) as target:
                    source.backup(target)
        applied = migrate_task_database(args.db)
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "applied": applied,
                    "backup": str(backup_path) if backup_path else None,
                }
            )
        )
    finally:
        lock.release()


if __name__ == "__main__":
    main()
