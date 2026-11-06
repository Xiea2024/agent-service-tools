"""Versioned, transactional upgrades for the existing Feishu task database."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1


def migrate_task_database(path: Path) -> bool:
    # rw refuses typos/missing files; the business store creates its own base tables.
    with (
        closing(sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)) as db,
        db,
    ):
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='feishu_tasks'").fetchone():
            raise ValueError("不是飞书任务数据库：缺少 feishu_tasks")
        db.execute("""CREATE TABLE IF NOT EXISTS task_schema_migrations (
            version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)""")
        version = db.execute(
            "SELECT COALESCE(MAX(version),0) FROM task_schema_migrations"
        ).fetchone()[0]
        if version > SCHEMA_VERSION:
            raise ValueError("任务数据库版本高于当前程序，不允许降级打开")
        if version == SCHEMA_VERSION:
            return False
        db.execute("""CREATE TABLE task_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            namespace TEXT NOT NULL, task_id TEXT NOT NULL, event_type TEXT NOT NULL,
            occurred_at TEXT NOT NULL, from_state TEXT, to_state TEXT,
            attempt INTEGER NOT NULL DEFAULT 0, error_code TEXT, details TEXT NOT NULL,
            actor TEXT, reason TEXT, dedupe_key TEXT,
            UNIQUE(namespace,task_id,dedupe_key),
            FOREIGN KEY(namespace,task_id) REFERENCES feishu_tasks(namespace,task_id)
        )""")
        db.execute("CREATE INDEX task_events_timeline ON task_events(namespace,task_id,event_id)")
        db.execute("CREATE INDEX task_list_time ON feishu_tasks(namespace,created_at,sequence)")
        db.execute(
            "INSERT INTO task_schema_migrations VALUES (?,?)",
            (SCHEMA_VERSION, datetime.now(UTC).isoformat()),
        )
        return True
