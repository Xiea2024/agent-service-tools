"""A single ordered Redis Streams worker with a durable SQLite inbox and recovery stages."""

import hashlib
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from typing import Any

from evaluation_jobs import WorkerLock
from observability.identifiers import task_id_for, trace_id_for
from task_audit.events import (
    append_event,
    emit_event,
    error_category,
    normalize_time,
    validate_recovery,
)
from task_audit.migrations import migrate_task_database

logger = logging.getLogger(__name__)
ACTIVE = ("queued", "invoking", "ready")
PROGRESS_RANK = {
    "queued": 0,
    "processing": 1,
    "needs_review": 2,
    "blocked": 2,
    "failed": 2,
    "answered": 3,
}


class DeliveryPending(Exception):
    """The notification thread has not finished publishing the saved answer."""


@dataclass(frozen=True)
class TaskProgress:
    task_id: str
    card_message_id: str | None
    desired_state: str
    snapshot_id: str | None
    version: int
    delivered_version: int
    attempts: int
    retry_at: float
    error: str | None


@dataclass(frozen=True)
class MessageTask:
    task_id: str
    event_key: str
    kind: str
    payload: dict[str, Any]
    state: str
    reply: dict[str, Any] | None
    attempts: int
    error: str | None
    chat_id: str
    context: dict[str, Any]
    created_at: str = ""
    updated_at: str = ""


class MessageTaskStore:
    def __init__(self, path: Path, namespace: str):
        self.path, self.namespace = path, namespace
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS feishu_tasks (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                namespace TEXT NOT NULL, task_id TEXT NOT NULL,
                event_key TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
                chat_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                reply TEXT, context TEXT, attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(namespace, event_key), UNIQUE(namespace, task_id)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS feishu_task_progress (
                namespace TEXT NOT NULL, task_id TEXT NOT NULL,
                card_message_id TEXT, desired_state TEXT NOT NULL DEFAULT 'queued',
                snapshot_id TEXT, version INTEGER NOT NULL DEFAULT 1,
                delivered_version INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
                error TEXT, PRIMARY KEY(namespace, task_id)
            )""")
        migrate_task_database(path)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _task(row) -> MessageTask:
        return MessageTask(
            row["task_id"],
            row["event_key"],
            row["kind"],
            json.loads(row["payload"]),
            row["state"],
            json.loads(row["reply"]) if row["reply"] else None,
            row["attempts"],
            row["error"],
            row["chat_id"],
            json.loads(row["context"]) if row["context"] else {},
            row["created_at"],
            row["updated_at"],
        )

    def record_invocation(self, event_key: str, thread_id: str, user_id: str):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT context FROM feishu_tasks WHERE namespace=? AND event_key=?",
                (self.namespace, event_key),
            ).fetchone()
            if row is None:
                raise ValueError("任务不存在")
            context = json.loads(row[0] or "{}")
            for key, value in (("thread_id", thread_id), ("user_id", user_id)):
                if context.get(key) not in (None, value):
                    raise ValueError("任务已关联不同会话或用户，不允许覆盖")
                context[key] = value
            context["trace_id"] = trace_id_for(task_id_for(self.namespace, event_key))
            db.execute(
                "UPDATE feishu_tasks SET context=? WHERE namespace=? AND event_key=?",
                (
                    json.dumps(context),
                    self.namespace,
                    event_key,
                ),
            )
            event = append_event(
                db,
                self.namespace,
                task_id_for(self.namespace, event_key),
                "agent_requested",
                dedupe_key="agent_requested",
            )
        emit_event(event)

    def record_agent_result(self, event_key: str, run_id: str, *, snapshot_id=None):
        if not run_id:
            return
        task_id = task_id_for(self.namespace, event_key)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT context FROM feishu_tasks WHERE namespace=? AND task_id=?",
                (self.namespace, task_id),
            ).fetchone()
            if row is None:
                raise ValueError("任务不存在")
            context = json.loads(row[0] or "{}")
            if context.get("run_id") not in (None, run_id):
                raise ValueError("任务已经关联其他 Agent 执行，不允许覆盖")
            context["run_id"] = run_id
            db.execute(
                "UPDATE feishu_tasks SET context=? WHERE namespace=? AND task_id=?",
                (json.dumps(context), self.namespace, task_id),
            )
            event = append_event(
                db,
                self.namespace,
                task_id,
                "agent_result_recovered" if snapshot_id else "agent_returned",
                dedupe_key="agent_returned",
                details={"snapshot_id": snapshot_id} if snapshot_id else None,
            )
        emit_event(event)

    def record_event(self, task_id, event_type, *, dedupe_key=None, details=None):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            event = append_event(
                db, self.namespace, task_id, event_type, dedupe_key=dedupe_key, details=details
            )
        emit_event(event)

    def get(self, task_id: str) -> MessageTask:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM feishu_tasks WHERE namespace=? AND task_id=?",
                (self.namespace, task_id),
            ).fetchone()
        if row is None:
            raise ValueError("任务不存在或不属于当前应用")
        return self._task(row)

    def enqueue(self, event_key: str, kind: str, payload: dict, capacity: int) -> MessageTask:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        task_id = task_id_for(self.namespace, event_key)
        now = datetime.now(UTC).isoformat()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM feishu_tasks WHERE namespace=? AND event_key=?",
                (self.namespace, event_key),
            ).fetchone()
            if old:
                if old["kind"] != kind or old["payload"] != encoded:
                    raise ValueError("同一事件的任务内容不一致")
                return self._task(old)
            count = db.execute(
                "SELECT count(*) FROM feishu_tasks WHERE namespace=? AND state IN (?,?,?)",
                (self.namespace, *ACTIVE),
            ).fetchone()[0]
            if count >= capacity:
                raise RuntimeError("飞书回复队列已满，请稍后重试")
            db.execute(
                """INSERT INTO feishu_tasks
                (namespace,task_id,event_key,kind,payload,chat_id,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?)""",
                (
                    self.namespace,
                    task_id,
                    event_key,
                    kind,
                    encoded,
                    payload.get("chat_id", ""),
                    now,
                    now,
                ),
            )
            event = append_event(
                db, self.namespace, task_id, "task_created", to_state="queued", dedupe_key="created"
            )
        emit_event(event)
        return self.get(task_id)

    def update(
        self,
        task_id: str,
        state: str,
        *,
        reply=None,
        error=None,
        attempted=False,
        reset_attempts=False,
        event_type=None,
    ):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            event = self._update(
                db,
                task_id,
                state,
                reply=reply,
                error=error,
                attempted=attempted,
                reset_attempts=reset_attempts,
                event_type=event_type,
            )
        emit_event(event)

    def _update(
        self,
        db,
        task_id,
        state,
        *,
        reply=None,
        error=None,
        attempted=False,
        reset_attempts=False,
        event_type=None,
        actor=None,
        reason=None,
        dedupe_key=None,
        details=None,
    ):
        row = db.execute(
            "SELECT * FROM feishu_tasks WHERE namespace=? AND task_id=?", (self.namespace, task_id)
        ).fetchone()
        if row is None:
            raise ValueError("任务不存在或不属于当前应用")
        old = self._task(row)
        if (
            old.state == state
            and (reply is None or reply == old.reply)
            and error == old.error
            and not attempted
            and not reset_attempts
            and actor is None
        ):
            return None
        db.execute(
            """UPDATE feishu_tasks SET state=?, reply=COALESCE(?,reply), error=?,
                attempts=CASE WHEN ? THEN 0 ELSE attempts+? END,
                updated_at=? WHERE namespace=? AND task_id=?""",
            (
                state,
                json.dumps(reply, ensure_ascii=False) if reply is not None else None,
                error,
                reset_attempts,
                int(attempted),
                datetime.now(UTC).isoformat(),
                self.namespace,
                task_id,
            ),
        )
        if reset_attempts:
            db.execute(
                """UPDATE feishu_task_progress SET attempts=0,retry_at=0,error=NULL,
                    version=version+1 WHERE namespace=? AND task_id=?""",
                (self.namespace, task_id),
            )
        new_attempt = 0 if reset_attempts else old.attempts + int(attempted)
        kind = event_type or {
            "invoking": "agent_started",
            "ready": "answer_saved",
            "succeeded": "task_succeeded",
            "failed": "task_failed",
            "needs_review": "needs_review",
            "blocked": "task_blocked",
            "abandoned": "task_abandoned",
        }.get(state, "state_changed")
        if attempted and not event_type:
            kind = "retry_exhausted" if state == "failed" else "retry_scheduled"
        refs = dict(details or {})
        if reply and reply.get("snapshot_id"):
            refs["snapshot_id"] = reply["snapshot_id"]
        return append_event(
            db,
            self.namespace,
            task_id,
            kind,
            from_state=old.state,
            to_state=state,
            attempt=new_attempt,
            error_code=error_category(state, reply or old.reply, error),
            details=refs,
            actor=actor,
            reason=reason,
            dedupe_key=dedupe_key,
        )

    def recover(self, task_id, action, *, actor, reason, operation_id, backend_checked=False):
        validate_recovery(action, actor, reason, operation_id)
        lock = WorkerLock(self.path.with_suffix(".worker.lock"))
        lock.acquire()
        try:
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                key = f"manual:{operation_id}"
                prior = db.execute(
                    """SELECT * FROM task_events WHERE namespace=? AND task_id=?
                    AND dedupe_key=?""",
                    (self.namespace, task_id, key),
                ).fetchone()
                if prior:
                    if (
                        prior["actor"] != actor.strip()
                        or prior["reason"] != reason.strip()
                        or json.loads(prior["details"]).get("action") != action
                    ):
                        raise ValueError("同一操作编号已经用于不同的恢复内容")
                    return self.get(task_id)
                row = db.execute(
                    "SELECT * FROM feishu_tasks WHERE namespace=? AND task_id=?",
                    (self.namespace, task_id),
                ).fetchone()
                if row is None:
                    raise ValueError("任务不存在或不属于当前应用")
                task = self._task(row)
                if action == "abandon":
                    if task.state != "needs_review" or not backend_checked:
                        raise ValueError(
                            "仅待确认任务可放弃，必须先核对后端并指定 --backend-checked"
                        )
                    state, error = "abandoned", "维护者核对后结束，不重新调用模型"
                else:
                    if task.state != "failed" or not task.reply:
                        raise ValueError("仅已有回答且发送失败的任务可重试发送")
                    state, error = "ready", None
                event = self._update(
                    db,
                    task_id,
                    state,
                    error=error,
                    reset_attempts=action == "retry-send",
                    event_type="manual_recovery",
                    actor=actor.strip(),
                    reason=reason.strip(),
                    dedupe_key=key,
                    details={"action": action, "operation_id": operation_id},
                )
            emit_event(event)
            return self.get(task_id)
        finally:
            lock.release()

    def ensure_progress(self, task_id: str):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                """INSERT OR IGNORE INTO feishu_task_progress(namespace,task_id)
                SELECT namespace,task_id FROM feishu_tasks WHERE namespace=? AND task_id=?
                AND kind='FeishuMessage' AND state IN (?,?,?)""",
                (self.namespace, task_id, *ACTIVE),
            ).rowcount
            event = (
                append_event(
                    db,
                    self.namespace,
                    task_id,
                    "notification_registered",
                    dedupe_key="notification_registered",
                )
                if changed
                else None
            )
        emit_event(event)

    def get_progress(self, task_id: str) -> TaskProgress | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM feishu_task_progress WHERE namespace=? AND task_id=?",
                (self.namespace, task_id),
            ).fetchone()
        return (
            TaskProgress(**{key: row[key] for key in TaskProgress.__dataclass_fields__})
            if row
            else None
        )

    def progress_tasks(self) -> list[MessageTask]:
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT t.* FROM feishu_tasks t JOIN feishu_task_progress p
                ON t.namespace=p.namespace AND t.task_id=p.task_id WHERE t.namespace=?
                AND (t.state IN (?,?,?) OR p.desired_state IN ('queued','processing')
                     OR (p.version>p.delivered_version AND p.attempts<3))
                ORDER BY t.sequence""",
                (self.namespace, *ACTIVE),
            ).fetchall()
        return [self._task(row) for row in rows]

    def set_progress_target(self, task_id: str, state: str, snapshot_id: str | None = None):
        if state not in PROGRESS_RANK:
            raise ValueError("未知卡片状态")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM feishu_task_progress WHERE namespace=? AND task_id=?",
                (self.namespace, task_id),
            ).fetchone()
            if not row:
                raise ValueError("任务未登记回复卡片")
            if PROGRESS_RANK[state] < PROGRESS_RANK[row["desired_state"]]:
                return
            if row["desired_state"] == state and row["snapshot_id"] == snapshot_id:
                return
            db.execute(
                """UPDATE feishu_task_progress SET desired_state=?,snapshot_id=?,
                version=version+1,attempts=0,retry_at=0,error=NULL
                WHERE namespace=? AND task_id=?""",
                (state, snapshot_id, self.namespace, task_id),
            )
            event = append_event(
                db,
                self.namespace,
                task_id,
                "notification_target",
                details={
                    "version": row["version"] + 1,
                    "target_state": state,
                    "snapshot_id": snapshot_id,
                },
            )
        emit_event(event)

    def bind_progress_card(self, task_id: str, message_id: str, chat_id: str):
        if not message_id or chat_id != self.get(task_id).chat_id:
            raise ValueError("进度卡片缺少编号或不属于原聊天")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT card_message_id FROM feishu_task_progress WHERE namespace=? AND task_id=?",
                (self.namespace, task_id),
            ).fetchone()
            changed = db.execute(
                """UPDATE feishu_task_progress SET card_message_id=?
                WHERE namespace=? AND task_id=? AND (card_message_id IS NULL OR card_message_id=?)""",
                (message_id, self.namespace, task_id, message_id),
            ).rowcount
            if not changed:
                raise ValueError("任务已绑定其他卡片")
            event = (
                append_event(
                    db,
                    self.namespace,
                    task_id,
                    "card_bound",
                    details={"card_message_id": message_id},
                )
                if old[0] is None
                else None
            )
        emit_event(event)

    def progress_attempt(self, task_id: str, version: int):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM feishu_task_progress WHERE namespace=? AND task_id=?",
                (self.namespace, task_id),
            ).fetchone()
            if row is None:
                raise ValueError("任务未登记回复卡片")
            event = append_event(
                db,
                self.namespace,
                task_id,
                "delivery_attempt",
                attempt=row["attempts"] + 1,
                details={"version": version, "target_state": row["desired_state"]},
            )
        emit_event(event)

    def progress_delivered(self, task_id: str, version: int):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM feishu_task_progress WHERE namespace=? AND task_id=?",
                (self.namespace, task_id),
            ).fetchone()
            if row is None or row["delivered_version"] >= version:
                return
            db.execute(
                """UPDATE feishu_task_progress SET delivered_version=MAX(delivered_version,?),
                error=CASE WHEN version=? THEN NULL ELSE error END
                WHERE namespace=? AND task_id=?""",
                (version, version, self.namespace, task_id),
            )
            event = append_event(
                db,
                self.namespace,
                task_id,
                "delivery_succeeded",
                attempt=row["attempts"] + 1,
                details={
                    "version": version,
                    "target_state": row["desired_state"]
                    if row["version"] == version
                    else "superseded",
                },
            )
        emit_event(event)

    def progress_failed(self, task_id: str, version: int, error: str, delay: float):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                """UPDATE feishu_task_progress SET attempts=attempts+1,error=?,retry_at=?
                WHERE namespace=? AND task_id=? AND version=?""",
                (error, time.time() + delay, self.namespace, task_id, version),
            ).rowcount
            event = None
            if changed:
                row = db.execute(
                    "SELECT attempts FROM feishu_task_progress WHERE namespace=? AND task_id=?",
                    (self.namespace, task_id),
                ).fetchone()
                event = append_event(
                    db,
                    self.namespace,
                    task_id,
                    "delivery_failed",
                    attempt=row[0],
                    error_code="delivery",
                    details={"version": version, "retry_at": time.time() + delay},
                )
        emit_event(event)

    def active(self) -> list[MessageTask]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM feishu_tasks WHERE namespace=? AND state IN (?,?,?) ORDER BY sequence",
                (self.namespace, *ACTIVE),
            ).fetchall()
        return [self._task(row) for row in rows]

    def recent(
        self, limit=20, *, since=None, until=None, state=None, kind=None, message_id=None
    ) -> list[MessageTask]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit 应为 1–1000")
        since, until = normalize_time(since), normalize_time(until)
        if since and until and since >= until:
            raise ValueError("起始时间必须早于结束时间")
        clauses, values = ["namespace=?"], [self.namespace]
        for column, operator, value in (
            ("created_at", ">=", since),
            ("created_at", "<", until),
            ("state", "=", state),
            ("kind", "=", kind),
            ("event_key", "=", message_id),
        ):
            if value is not None:
                clauses.append(f"{column}{operator}?")
                values.append(value)
        with closing(self._connect()) as db:
            rows = db.execute(
                f"SELECT * FROM feishu_tasks WHERE {' AND '.join(clauses)} ORDER BY sequence DESC LIMIT ?",
                (*values, limit),
            ).fetchall()
        return [self._task(row) for row in rows]

    def timeline(self, task_id, *, after_event_id=0, limit=200):
        self.get(task_id)
        if after_event_id < 0 or not 1 <= limit <= 1000:
            raise ValueError("事件游标不能为负，事件查询数量应为 1–1000")
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT * FROM task_events WHERE namespace=? AND task_id=?
                AND event_id>? ORDER BY event_id LIMIT ?""",
                (self.namespace, task_id, after_event_id, limit),
            ).fetchall()
        return [{**dict(row), "details": json.loads(row["details"])} for row in rows]

    def uncertain_chat(self, chat_id: str, task_id: str) -> bool:
        if not chat_id:
            return False
        with closing(self._connect()) as db:
            return (
                db.execute(
                    """SELECT 1 FROM feishu_tasks WHERE namespace=? AND chat_id=?
                AND state='needs_review' AND task_id<>? LIMIT 1""",
                    (self.namespace, chat_id, task_id),
                ).fetchone()
                is not None
            )


class RedisTaskStream:
    ENQUEUE = """
    local old = redis.call('GET', KEYS[2])
    if old and #redis.call('XRANGE', KEYS[1], old, old) > 0 then return old end
    local id = redis.call('XADD', KEYS[1], '*', 'task_id', ARGV[1])
    redis.call('SET', KEYS[2], id, 'EX', ARGV[2])
    return id
    """

    def __init__(self, client: Any, namespace: str, ttl: int = 86400):
        digest = hashlib.sha256(namespace.encode()).hexdigest()[:24]
        self.client, self.ttl = client, ttl
        self.stream = f"supportpilot:{{{digest}}}:tasks"
        self.group, self.consumer = "feishu", "single-worker"

    @classmethod
    def connect(cls, url: str, namespace: str, ttl: int):
        from redis import Redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry

        return cls(
            Redis.from_url(
                url,
                decode_responses=True,
                socket_timeout=1,
                socket_connect_timeout=1,
                retry_on_timeout=False,
                retry=Retry(NoBackoff(), 0),
            ),
            namespace,
            ttl,
        )

    def initialize(self):
        from redis.exceptions import ResponseError

        self.client.ping()
        try:
            self.client.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def publish(self, task_id: str) -> str:
        return self.client.eval(
            self.ENQUEUE, 2, self.stream, f"{self.stream}:dedup:{task_id}", task_id, self.ttl
        )

    def take(self, task_id: str) -> str:
        # The SQLite sequence also preserves order after Redis loses an unsynced AOF tail.
        entry_id = self.publish(task_id)
        claimed = self.client.xclaim(self.stream, self.group, self.consumer, 0, [entry_id])
        while not claimed:
            rows = self.client.xreadgroup(self.group, self.consumer, {self.stream: ">"}, count=32)
            if not rows:
                raise RuntimeError("任务流与本地台账不一致")
            claimed = self.client.xclaim(self.stream, self.group, self.consumer, 0, [entry_id])
        return entry_id

    def finish(self, entry_id: str):
        with self.client.pipeline(transaction=True) as pipe:
            pipe.xack(self.stream, self.group, entry_id)
            pipe.xdel(self.stream, entry_id)
            pipe.execute()

    def cleanup(self):
        self.client.xreadgroup(self.group, self.consumer, {self.stream: ">"}, count=32)
        rows = self.client.xreadgroup(self.group, self.consumer, {self.stream: "0"}, count=32)
        return rows[0][1] if rows else []

    def close(self):
        self.client.close()


class DurableTaskWorker:
    def __init__(
        self,
        store: MessageTaskStore,
        stream: RedisTaskStream,
        dispatch: Callable[[MessageTask], None],
        max_attempts=3,
        retry_delay=2,
        on_saved: Callable[[MessageTask], None] | None = None,
    ):
        self.store, self.stream, self.dispatch = store, stream, dispatch
        self.max_attempts, self.retry_delay = max_attempts, retry_delay
        self.on_saved = on_saved
        self.last_poll_timestamp = 0.0
        self.lock = WorkerLock(store.path.with_suffix(".worker.lock"))
        self.stop_event = Event()
        self.thread = Thread(target=self._run, name="feishu-stream-worker", daemon=True)

    def enqueue(self, event_key: str, kind: str, payload: dict):
        task = self.store.enqueue(event_key, kind, payload, capacity=32)
        if self.on_saved:
            self.on_saved(task)
        if task.state in ACTIVE:
            try:
                self.stream.publish(task.task_id)
            except Exception:
                # The committed inbox is an outbox too; the worker republishes after recovery.
                raise RuntimeError("Redis 暂不可用，任务已保存，恢复后继续处理") from None
        return task

    def start(self, before_start: Callable[[], None] | None = None):
        try:
            self.lock.acquire()
        except RuntimeError:
            raise RuntimeError("已有进程持有飞书任务工作器，请停止旧飞书入口后再启动") from None
        try:
            self.stream.initialize()
            if before_start:
                before_start()
            from uuid import uuid4

            startup_id = str(uuid4())
            for task in self.store.active():
                self.store.record_event(
                    task.task_id,
                    "recovery_scheduled",
                    dedupe_key=f"startup:{startup_id}",
                    details={"observed_state": task.state},
                )
            self.thread.start()
        except BaseException:
            self.lock.release()
            self.stream.close()
            raise

    def stop(self, after_join: Callable[[], None] | None = None):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join()
        try:
            if after_join:
                after_join()
        finally:
            self.stream.close()
            self.lock.release()

    def step(self) -> bool:
        self.stream.initialize()
        active = self.store.active()
        for task in active:
            self.stream.publish(task.task_id)
        for entry_id, fields in self.stream.cleanup():
            task = self.store.get(fields["task_id"])
            if task.state not in ACTIVE:
                self.stream.finish(entry_id)
        self.last_poll_timestamp = time.time()
        if not active:
            return False
        task = active[0]
        entry_id = self.stream.take(task.task_id)
        self.store.record_event(
            task.task_id,
            "dispatch_started",
            dedupe_key=f"dispatch:{task.state}:{task.attempts}",
            details={"observed_state": task.state},
        )
        logger.info(
            "任务开始：task_id=%s state=%s attempts=%s", task.task_id, task.state, task.attempts
        )
        try:
            self.dispatch(task)
        except DeliveryPending:
            return False
        except Exception as exc:
            current = self.store.get(task.task_id)
            if current.state not in ACTIVE:
                logger.exception(
                    "终态任务通知失败：task_id=%s state=%s", task.task_id, current.state
                )
                return True
            if current.state == "invoking":
                self.store.update(task.task_id, "needs_review", error="执行结果不明，禁止自动重试")
            else:
                exhausted = current.attempts + 1 >= self.max_attempts
                self.store.update(
                    task.task_id,
                    "failed" if exhausted else current.state,
                    error=type(exc).__name__,
                    attempted=True,
                )
            logger.exception("任务处理失败：task_id=%s", task.task_id)
            return True
        current = self.store.get(task.task_id)
        if current.state in ACTIVE:
            self.store.update(task.task_id, "succeeded")
        self.stream.finish(entry_id)
        logger.info(
            "任务结束：task_id=%s state=%s", task.task_id, self.store.get(task.task_id).state
        )
        return True

    def _run(self):
        while not self.stop_event.is_set():
            try:
                worked = self.step()
            except Exception:
                logger.exception("Redis 任务工作器暂不可用，任务台账保留")
                worked = True
            self.stop_event.wait(self.retry_delay if worked else 0.2)
