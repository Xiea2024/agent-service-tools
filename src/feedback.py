"""Persist immutable answer snapshots and first-submission feedback in scoped SQLite tables."""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

Rating = Literal["helpful", "not_helpful"]
MAX_COMMENT_LENGTH = 1000


@dataclass(frozen=True)
class AnswerSnapshot:
    snapshot_id: str
    source_message_id: str
    chat_id: str
    open_id: str
    user_id: str
    thread_id: str
    run_id: str
    question: str
    answer: str
    created_at: str
    card_message_id: str | None = None


@dataclass(frozen=True)
class AnswerFeedback:
    snapshot_id: str
    event_id: str
    rating: Rating
    comment: str
    submitted_at: str


class FeedbackStore:
    def __init__(self, path: Path, app_id: str, agent_id: str):
        self.path = path
        self.namespace = json.dumps([app_id, agent_id])
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS answer_snapshots (
                    snapshot_id TEXT PRIMARY KEY, namespace TEXT NOT NULL,
                    source_message_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                    open_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    question TEXT NOT NULL, answer TEXT NOT NULL, created_at TEXT NOT NULL,
                    card_message_id TEXT,
                    UNIQUE(namespace, source_message_id), UNIQUE(namespace, card_message_id)
                );
                CREATE TABLE IF NOT EXISTS answer_feedback (
                    snapshot_id TEXT PRIMARY KEY REFERENCES answer_snapshots(snapshot_id),
                    namespace TEXT NOT NULL, event_id TEXT NOT NULL,
                    rating TEXT NOT NULL CHECK(rating IN ('helpful', 'not_helpful')),
                    comment TEXT NOT NULL CHECK(length(comment) <= 1000),
                    submitted_at TEXT NOT NULL, UNIQUE(namespace, event_id)
                );
            """)

    @contextmanager
    def _transaction(self):
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

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> AnswerSnapshot:
        return AnswerSnapshot(**{key: row[key] for key in AnswerSnapshot.__dataclass_fields__})

    @staticmethod
    def _feedback(row: sqlite3.Row) -> AnswerFeedback:
        return AnswerFeedback(**{key: row[key] for key in AnswerFeedback.__dataclass_fields__})

    def get_answer(self, snapshot_id: str) -> AnswerSnapshot:
        with self._transaction() as db:
            return self._get_answer(db, snapshot_id)

    def _get_answer(self, db: sqlite3.Connection, snapshot_id: str) -> AnswerSnapshot:
        row = db.execute(
            "SELECT * FROM answer_snapshots WHERE namespace=? AND snapshot_id=?",
            (self.namespace, snapshot_id),
        ).fetchone()
        if not row:
            raise ValueError("回答记录不存在或不属于当前应用")
        return self._snapshot(row)

    def find_answer(
        self, source_message_id: str, chat_id: str, open_id: str, question: str
    ) -> AnswerSnapshot | None:
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM answer_snapshots WHERE namespace=? AND source_message_id=?",
                (self.namespace, source_message_id),
            ).fetchone()
            if not row:
                return None
            result = self._snapshot(row)
            if (result.chat_id, result.open_id, result.question) != (chat_id, open_id, question):
                raise ValueError("原消息编号对应的用户、聊天或问题不一致")
            return result

    def save_answer(
        self,
        *,
        source_message_id: str,
        chat_id: str,
        open_id: str,
        user_id: str,
        thread_id: str,
        run_id: str,
        question: str,
        answer: str,
    ) -> AnswerSnapshot:
        values = (source_message_id, chat_id, open_id, user_id, thread_id, run_id, question, answer)
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("回答快照缺少消息、用户、运行标识或正文")
        with self._transaction() as db:
            old = db.execute(
                "SELECT * FROM answer_snapshots WHERE namespace=? AND source_message_id=?",
                (self.namespace, source_message_id),
            ).fetchone()
            if old:
                snapshot = self._snapshot(old)
                if (
                    snapshot.source_message_id,
                    snapshot.chat_id,
                    snapshot.open_id,
                    snapshot.user_id,
                    snapshot.thread_id,
                    snapshot.run_id,
                    snapshot.question,
                    snapshot.answer,
                ) != values:
                    raise ValueError("原消息已经关联了不同的回答，不能覆盖快照")
                return snapshot
            snapshot_id = str(uuid4())
            db.execute(
                """INSERT INTO answer_snapshots
                (snapshot_id, namespace, source_message_id, chat_id, open_id, user_id,
                 thread_id, run_id, question, answer, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (snapshot_id, self.namespace, *values, datetime.now(UTC).isoformat()),
            )
            return self._get_answer(db, snapshot_id)

    def bind_card(self, snapshot_id: str, card_message_id: str, chat_id: str) -> None:
        if not isinstance(card_message_id, str) or not card_message_id.strip():
            raise ValueError("卡片响应缺少消息编号")
        with self._transaction() as db:
            snapshot = self._get_answer(db, snapshot_id)
            if snapshot.chat_id != chat_id or snapshot.card_message_id not in {
                None,
                card_message_id,
            }:
                raise ValueError("卡片不属于原聊天，或回答已经绑定了其他卡片")
            db.execute(
                "UPDATE answer_snapshots SET card_message_id=? WHERE namespace=? AND snapshot_id=?",
                (card_message_id, self.namespace, snapshot_id),
            )

    def get_feedback(self, snapshot_id: str) -> AnswerFeedback | None:
        with self._transaction() as db:
            self._get_answer(db, snapshot_id)
            row = db.execute(
                "SELECT * FROM answer_feedback WHERE namespace=? AND snapshot_id=?",
                (self.namespace, snapshot_id),
            ).fetchone()
            return self._feedback(row) if row else None

    def recent_feedback(self, limit: int = 20) -> list[AnswerFeedback]:
        if not 1 <= limit <= 100:
            raise ValueError("查询数量应为 1–100")
        with self._transaction() as db:
            rows = db.execute(
                """SELECT * FROM answer_feedback WHERE namespace=?
                ORDER BY submitted_at DESC, snapshot_id LIMIT ?""",
                (self.namespace, limit),
            ).fetchall()
            return [self._feedback(row) for row in rows]

    def submit(
        self,
        *,
        snapshot_id: str,
        open_id: str,
        chat_id: str,
        card_message_id: str,
        event_id: str,
        rating: Rating,
        comment: str = "",
    ) -> AnswerFeedback:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (
                snapshot_id,
                open_id,
                chat_id,
                card_message_id,
                event_id,
            )
        ):
            raise ValueError("反馈缺少事件、用户或卡片编号")
        if rating not in {"helpful", "not_helpful"}:
            raise ValueError("请选择有帮助或没帮助")
        if not isinstance(comment, str) or len(comment) > MAX_COMMENT_LENGTH:
            raise ValueError("意见最多 1000 字")
        with self._transaction() as db:
            snapshot = self._get_answer(db, snapshot_id)
            if (snapshot.open_id, snapshot.chat_id, snapshot.card_message_id) != (
                open_id,
                chat_id,
                card_message_id,
            ):
                raise ValueError("只能评价你与机器人原单聊中的这条回答")
            event = db.execute(
                "SELECT snapshot_id FROM answer_feedback WHERE namespace=? AND event_id=?",
                (self.namespace, event_id),
            ).fetchone()
            if event and event["snapshot_id"] != snapshot_id:
                raise ValueError("事件编号已经用于另一条回答")
            old = db.execute(
                "SELECT * FROM answer_feedback WHERE namespace=? AND snapshot_id=?",
                (self.namespace, snapshot_id),
            ).fetchone()
            if old:
                return self._feedback(old)
            feedback = AnswerFeedback(
                snapshot_id, event_id, rating, comment.strip(), datetime.now(UTC).isoformat()
            )
            db.execute(
                "INSERT INTO answer_feedback VALUES (?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    self.namespace,
                    event_id,
                    rating,
                    feedback.comment,
                    feedback.submitted_at,
                ),
            )
            return feedback
