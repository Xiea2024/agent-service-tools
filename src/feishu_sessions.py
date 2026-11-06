"""飞书会话管理的 SQL 层：保存聊天映射、会话列表和当前选择。

本模块不保存模型消息；消息仍由后端的 LangGraph checkpoint 管理。
SQLite 使用独立业务表与参数化 SQL，切换会话和事件登记在同一事务中完成。
"""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5


def legacy_thread_id(app_id: str, agent_id: str, chat_id: str) -> str:
    """保留原 UUID5 算法，首次启用管理功能时仍能找到此前的 checkpoint。"""
    return str(uuid5(NAMESPACE_URL, json.dumps(["feishu", app_id, agent_id, chat_id])))


@dataclass(frozen=True)
class FeishuSession:
    """卡片所需的业务元信息；turn_count 只统计收到有效 AI 回复的轮次。"""

    thread_id: str
    title: str
    created_at: str
    last_used_at: str
    turn_count: int


class FeishuSessionStore:
    """每次操作使用独立连接，可跨线程使用；数据库文件由调用方配置。"""

    def __init__(self, path: Path, app_id: str, agent_id: str):
        self.path = path
        self.app_id, self.agent_id = app_id, agent_id
        self.namespace = json.dumps([app_id, agent_id])
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            # chats 管当前映射，sessions 管历史列表，actions 防止重复点击事件重建会话。
            # 不读写 LangGraph 自己的 checkpoint 表。
            db.executescript("""
                CREATE TABLE IF NOT EXISTS feishu_chats (
                    namespace TEXT NOT NULL, chat_id TEXT NOT NULL,
                    open_id TEXT, active_thread TEXT,
                    PRIMARY KEY (namespace, chat_id), UNIQUE (namespace, open_id)
                );
                CREATE TABLE IF NOT EXISTS feishu_sessions (
                    thread_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, chat_id TEXT NOT NULL,
                    title TEXT NOT NULL, created_at TEXT NOT NULL, last_used_at TEXT NOT NULL,
                    turn_count INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (namespace, chat_id) REFERENCES feishu_chats(namespace, chat_id)
                );
                CREATE INDEX IF NOT EXISTS feishu_sessions_chat_time
                    ON feishu_sessions(namespace, chat_id, last_used_at DESC);
                CREATE TABLE IF NOT EXISTS feishu_session_actions (
                    namespace TEXT NOT NULL, event_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                    action TEXT NOT NULL, target_thread TEXT NOT NULL,
                    PRIMARY KEY (namespace, event_id)
                );
            """)

    @contextmanager
    def _transaction(self):
        """BEGIN IMMEDIATE 串行化读后写，出错自动回滚；离开时显式关闭连接。"""
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _ensure_chat(self, db: sqlite3.Connection, chat_id: str, open_id: str = "") -> None:
        if not chat_id:
            raise ValueError("缺少飞书 chat_id")
        db.execute(
            "INSERT OR IGNORE INTO feishu_chats(namespace, chat_id) VALUES (?, ?)",
            (self.namespace, chat_id),
        )
        row = db.execute(
            "SELECT open_id, active_thread FROM feishu_chats WHERE namespace=? AND chat_id=?",
            (self.namespace, chat_id),
        ).fetchone()
        if open_id:
            if row["open_id"] and row["open_id"] != open_id:
                raise ValueError("该单聊不属于当前用户")
            db.execute(
                "UPDATE feishu_chats SET open_id=? WHERE namespace=? AND chat_id=?",
                (open_id, self.namespace, chat_id),
            )
        if not row["active_thread"]:
            thread_id = legacy_thread_id(self.app_id, self.agent_id, chat_id)
            now = datetime.now(UTC).isoformat()
            db.execute(
                "INSERT INTO feishu_sessions VALUES (?, ?, ?, ?, ?, ?, 0)",
                (thread_id, self.namespace, chat_id, "原有会话", now, now),
            )
            db.execute(
                "UPDATE feishu_chats SET active_thread=? WHERE namespace=? AND chat_id=?",
                (thread_id, self.namespace, chat_id),
            )

    def _current(self, db: sqlite3.Connection, chat_id: str) -> FeishuSession:
        row = db.execute(
            """SELECT s.thread_id, s.title, s.created_at, s.last_used_at, s.turn_count
            FROM feishu_sessions s JOIN feishu_chats c
              ON s.thread_id=c.active_thread AND s.namespace=c.namespace AND s.chat_id=c.chat_id
            WHERE c.namespace=? AND c.chat_id=?""",
            (self.namespace, chat_id),
        ).fetchone()
        if row is None:
            raise ValueError("当前会话记录不存在")
        return FeishuSession(**dict(row))

    def current(self, chat_id: str, open_id: str = "") -> FeishuSession:
        """第一次登记使用旧线程；之后读取持久化的当前选择，不重新计算覆盖它。"""
        with self._transaction() as db:
            self._ensure_chat(db, chat_id, open_id)
            return self._current(db, chat_id)

    def chat_for_user(self, open_id: str) -> str | None:
        """菜单事件没有 chat_id，优先使用之前登记的单聊映射。"""
        with self._transaction() as db:
            row = db.execute(
                "SELECT chat_id FROM feishu_chats WHERE namespace=? AND open_id=?",
                (self.namespace, open_id),
            ).fetchone()
            return row["chat_id"] if row else None

    def change(
        self, chat_id: str, event_id: str, target_thread: str | None = None
    ) -> FeishuSession:
        """target 为空则新建，否则继续旧会话；同一事件重投不重复变更当前选择。"""
        if not event_id:
            raise ValueError("缺少事件 ID，不能可靠地识别重复操作")
        with self._transaction() as db:
            self._ensure_chat(db, chat_id)
            old = db.execute(
                "SELECT chat_id, action, target_thread FROM feishu_session_actions WHERE namespace=? AND event_id=?",
                (self.namespace, event_id),
            ).fetchone()
            action, requested = ("switch", target_thread) if target_thread else ("new", "")
            if old:
                if (old["chat_id"], old["action"], old["target_thread"]) != (
                    chat_id,
                    action,
                    requested,
                ):
                    raise ValueError("事件 ID 对应的操作不一致")
                return self._current(db, chat_id)
            now = datetime.now(UTC).isoformat()
            if target_thread:
                # 所有权由数据库作用域验证，不信任卡片提交的线程编号。
                owned = db.execute(
                    "SELECT 1 FROM feishu_sessions WHERE namespace=? AND chat_id=? AND thread_id=?",
                    (self.namespace, chat_id, target_thread),
                ).fetchone()
                if not owned:
                    raise ValueError("不能切换到其他聊天的会话，或该会话已经不存在")
                thread_id = target_thread
                db.execute(
                    "UPDATE feishu_sessions SET last_used_at=? WHERE thread_id=?", (now, thread_id)
                )
            else:
                thread_id = str(uuid4())
                db.execute(
                    "INSERT INTO feishu_sessions VALUES (?, ?, ?, ?, ?, ?, 0)",
                    (thread_id, self.namespace, chat_id, "新会话", now, now),
                )
            db.execute(
                "UPDATE feishu_chats SET active_thread=? WHERE namespace=? AND chat_id=?",
                (thread_id, self.namespace, chat_id),
            )
            db.execute(
                "INSERT INTO feishu_session_actions VALUES (?, ?, ?, ?, ?)",
                (self.namespace, event_id, chat_id, action, requested),
            )
            return self._current(db, chat_id)

    def recent(self, chat_id: str, limit: int = 8) -> list[FeishuSession]:
        """首版卡片仅列最近 8 个会话，SQL 同时限制作用域和返回数量。"""
        if not 1 <= limit <= 20:
            raise ValueError("会话列表数量必须在 1–20 之间")
        with self._transaction() as db:
            self._ensure_chat(db, chat_id)
            rows = db.execute(
                """SELECT thread_id, title, created_at, last_used_at, turn_count
                FROM feishu_sessions WHERE namespace=? AND chat_id=?
                ORDER BY last_used_at DESC LIMIT ?""",
                (self.namespace, chat_id, limit),
            ).fetchall()
            return [FeishuSession(**dict(row)) for row in rows]

    def record_turn(self, chat_id: str, thread_id: str, text: str) -> None:
        """成功回复后记录该线程；不因记录旧线程结果而覆盖当前选择。"""
        title = " ".join(text.split())[:40] or "未命名会话"
        with self._transaction() as db:
            result = db.execute(
                """UPDATE feishu_sessions
                SET title=CASE WHEN turn_count=0 AND title='新会话' THEN ? ELSE title END,
                    turn_count=turn_count+1, last_used_at=?
                WHERE namespace=? AND chat_id=? AND thread_id=?""",
                (title, datetime.now(UTC).isoformat(), self.namespace, chat_id, thread_id),
            )
            if result.rowcount != 1:
                raise ValueError("会话不属于当前聊天")
