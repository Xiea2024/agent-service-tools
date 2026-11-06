"""Durable single-job scheduling around the existing evaluation runner."""

import asyncio
import json
import logging
import os
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from typing import Any
from uuid import uuid4

from client import AgentClient
from evaluation import Suite, run_evaluation, timestamp

logger = logging.getLogger(__name__)
ACTIVE = ("queued", "running", "publishing")
TERMINAL = ("succeeded", "publish_failed", "failed", "interrupted")


@dataclass(frozen=True)
class EvaluationJob:
    job_id: str
    owner_open_id: str
    request_event_id: str
    status: str
    output_dir: str
    suite_json: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    summary_json: str | None
    document_id: str | None
    report_url: str | None
    error_stage: str | None
    error_message: str | None
    notified_at: str | None
    notification_error: str | None

    @property
    def summary(self) -> dict[str, Any]:
        return json.loads(self.summary_json) if self.summary_json else {}


class JobBusyError(ValueError):
    pass


class EvaluationJobStore:
    def __init__(self, path: Path, app_id: str, agent_id: str):
        self.path = path
        self.namespace = json.dumps([app_id, agent_id])
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS evaluation_jobs (
                    job_id TEXT PRIMARY KEY, namespace TEXT NOT NULL,
                    owner_open_id TEXT NOT NULL, request_event_id TEXT NOT NULL,
                    status TEXT NOT NULL, output_dir TEXT NOT NULL, suite_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                    summary_json TEXT, document_id TEXT, report_url TEXT,
                    error_stage TEXT, error_message TEXT, notified_at TEXT, notification_error TEXT,
                    UNIQUE(namespace, request_event_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS evaluation_jobs_one_active
                    ON evaluation_jobs(namespace) WHERE status IN ('queued','running','publishing');
            """)

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _job(row: sqlite3.Row) -> EvaluationJob:
        return EvaluationJob(**{key: row[key] for key in EvaluationJob.__dataclass_fields__})

    def create(
        self, event_id: str, owner: str, suite: Suite, output_root: Path
    ) -> tuple[EvaluationJob, bool]:
        if not event_id.strip() or not owner.strip():
            raise ValueError("任务缺少事件编号或发起人")
        with self._transaction() as db:
            old = db.execute(
                "SELECT * FROM evaluation_jobs WHERE namespace=? AND request_event_id=?",
                (self.namespace, event_id),
            ).fetchone()
            if old:
                if old["owner_open_id"] != owner:
                    raise ValueError("任务不属于当前用户")
                return self._job(old), False
            active = db.execute(
                "SELECT * FROM evaluation_jobs WHERE namespace=? AND status IN ('queued','running','publishing')",
                (self.namespace,),
            ).fetchone()
            if active:
                if active["owner_open_id"] != owner:
                    raise JobBusyError("当前已有评测任务执行，请稍后再试")
                return self._job(active), False
            job_id = str(uuid4())
            db.execute(
                """INSERT INTO evaluation_jobs
                (job_id, namespace, owner_open_id, request_event_id, status, output_dir, suite_json, created_at)
                VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)""",
                (
                    job_id,
                    self.namespace,
                    owner,
                    event_id,
                    str(output_root / job_id),
                    suite.model_dump_json(),
                    timestamp(),
                ),
            )
            return self._job(
                db.execute("SELECT * FROM evaluation_jobs WHERE job_id=?", (job_id,)).fetchone()
            ), True

    def get(self, job_id: str, owner: str | None = None) -> EvaluationJob:
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM evaluation_jobs WHERE namespace=? AND job_id=?",
                (self.namespace, job_id),
            ).fetchone()
            if not row or (owner is not None and row["owner_open_id"] != owner):
                raise ValueError("评测任务不存在或不属于当前用户")
            return self._job(row)

    def latest(self, owner: str) -> EvaluationJob | None:
        with self._transaction() as db:
            row = db.execute(
                """SELECT * FROM evaluation_jobs WHERE namespace=? AND owner_open_id=?
                ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (self.namespace, owner),
            ).fetchone()
            return self._job(row) if row else None

    def claim(self) -> EvaluationJob | None:
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM evaluation_jobs WHERE namespace=? AND status='queued' LIMIT 1",
                (self.namespace,),
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE evaluation_jobs SET status='running', started_at=? WHERE job_id=?",
                (timestamp(), row["job_id"]),
            )
            values = dict(row)
            values.update(status="running", started_at=timestamp())
            return EvaluationJob(**{key: values[key] for key in EvaluationJob.__dataclass_fields__})

    def update(self, job_id: str, **values: Any) -> EvaluationJob:
        allowed = {
            "status",
            "finished_at",
            "summary_json",
            "document_id",
            "report_url",
            "error_stage",
            "error_message",
            "notified_at",
            "notification_error",
        }
        if not values or not values.keys() <= allowed:
            raise ValueError("无效的任务更新字段")
        if "status" in values and values["status"] not in (*ACTIVE, *TERMINAL):
            raise ValueError("无效任务状态")
        with self._transaction() as db:
            changed = db.execute(
                f"UPDATE evaluation_jobs SET {', '.join(f'{key}=?' for key in values)} WHERE namespace=? AND job_id=?",
                (*values.values(), self.namespace, job_id),
            ).rowcount
            if changed != 1:
                raise ValueError("评测任务不存在")
        return self.get(job_id)

    def recover(self) -> None:
        with self._transaction() as db:
            db.execute(
                """UPDATE evaluation_jobs SET status='interrupted', finished_at=?,
                error_stage='restart', error_message='进程已重启，原任务中断；已有本地产物保留，未自动重跑'
                WHERE namespace=? AND status IN ('queued','running','publishing')""",
                (timestamp(), self.namespace),
            )

    def pending_notifications(self) -> list[EvaluationJob]:
        with self._transaction() as db:
            rows = db.execute(
                """SELECT * FROM evaluation_jobs WHERE namespace=? AND notified_at IS NULL
                AND status IN ('succeeded','publish_failed','failed','interrupted')""",
                (self.namespace,),
            ).fetchall()
            return [self._job(row) for row in rows]


class WorkerLock:
    def __init__(self, path: Path):
        self.path = path
        self.stream = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise RuntimeError("已有进程持有评测工作器，请停止旧飞书入口后再启动") from None
        self.stream = stream

    def release(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None


async def evaluate_job(
    job: EvaluationJob,
    stop: Event,
    base_url: str,
    auth_secret: str | None,
    model: str | None = None,
    timeout: float = 90,
) -> dict[str, Any]:
    client = AgentClient(base_url=base_url, timeout=min(timeout, 5), get_info=False)
    client.auth_secret = auth_secret
    await asyncio.to_thread(client.retrieve_info)
    client.update_agent("dev-issue-agent")
    if model and client.info and model not in client.info.models:
        raise ValueError(f"服务未提供模型：{model}")
    client.timeout = timeout
    task = asyncio.create_task(
        run_evaluation(
            Suite.model_validate_json(job.suite_json), client, Path(job.output_dir), model, timeout
        )
    )

    async def watch_shutdown() -> None:
        while not stop.is_set():
            await asyncio.sleep(0.1)
        task.cancel()

    watcher = asyncio.create_task(watch_shutdown())
    try:
        return await task
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


class EvaluationWorker:
    def __init__(
        self,
        store: EvaluationJobStore,
        evaluate: Callable[[EvaluationJob, Event], dict[str, Any]],
        publish: Callable[[EvaluationJob, Event], str],
        notify: Callable[[EvaluationJob], None],
    ):
        self.store, self.evaluate, self.publish, self.notify = store, evaluate, publish, notify
        self.stop_event = Event()
        self.wake = Event()
        self.lock = WorkerLock(store.path.with_suffix(".worker.lock"))
        self.thread: Thread | None = None

    def start(self) -> None:
        self.lock.acquire()
        try:
            self.store.recover()
            self.thread = Thread(target=self._run, name="supportpilot-evaluation", daemon=True)
            self.thread.start()
        except BaseException:
            self.lock.release()
            raise

    def stop(self) -> None:
        self.stop_event.set()
        self.wake.set()
        if self.thread:
            self.thread.join()
        self.lock.release()

    def notify_saved(self, job: EvaluationJob) -> None:
        try:
            self.notify(job)
        except Exception as exc:
            logger.exception("评测通知发送失败：job_id=%s", job.job_id)
            self.store.update(job.job_id, notification_error=f"{type(exc).__name__}: {exc}")
        else:
            self.store.update(job.job_id, notified_at=timestamp(), notification_error=None)

    def _run(self) -> None:
        for job in self.store.pending_notifications():
            if self.stop_event.is_set():
                return
            self.notify_saved(job)
        while not self.stop_event.is_set():
            try:
                job = self.store.claim()
                if job:
                    self.execute(job)
                    continue
            except Exception:
                logger.exception("评测工作器处理失败")
            self.wake.wait(0.5)
            self.wake.clear()

    def execute(self, job: EvaluationJob) -> None:
        stage = "evaluation"
        try:
            result = self.evaluate(job, self.stop_event)
            job = self.store.update(
                job.job_id, status="publishing", summary_json=json.dumps(result, ensure_ascii=False)
            )
            stage = "publication"
            url = self.publish(job, self.stop_event)
            self.store.update(
                job.job_id, status="succeeded", report_url=url, finished_at=timestamp()
            )
        except asyncio.CancelledError:
            self.store.update(
                job.job_id,
                status="interrupted",
                error_stage=stage,
                error_message="进程停止，任务中断；已有本地产物保留",
                finished_at=timestamp(),
            )
        except Exception as exc:
            logger.exception("评测任务失败：job_id=%s stage=%s", job.job_id, stage)
            self.store.update(
                job.job_id,
                status="publish_failed" if stage == "publication" else "failed",
                error_stage=stage,
                error_message=f"{type(exc).__name__}: {exc}",
                finished_at=timestamp(),
            )
        if not self.stop_event.is_set():
            self.notify_saved(self.store.get(job.job_id))
