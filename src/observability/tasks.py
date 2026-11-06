"""Read-only task gauges survive process restarts and do not alter business tables."""

import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime

from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import GaugeMetricFamily

STATES = (
    "queued",
    "invoking",
    "ready",
    "succeeded",
    "failed",
    "needs_review",
    "blocked",
    "abandoned",
)


class TaskCollector:
    def __init__(self, worker):
        self.worker = worker

    def collect(self):
        worker = self.worker
        alive = GaugeMetricFamily("supportpilot_worker_alive", "Worker thread is alive.")
        alive.add_metric([], int(worker.thread.is_alive()))
        yield alive
        poll = GaugeMetricFamily(
            "supportpilot_worker_last_poll_timestamp_seconds", "Last successful Redis/ledger poll."
        )
        poll.add_metric([], worker.last_poll_timestamp)
        yield poll
        available = GaugeMetricFamily("supportpilot_task_store_up", "Task ledger readable.")
        try:
            # Read-only URI: a missing/wrong path must not silently create an empty database.
            uri = worker.store.path.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
                db.execute("BEGIN")
                rows = db.execute(
                    """SELECT state,count(*),MIN(created_at),MAX(updated_at) FROM feishu_tasks
                    WHERE namespace=? AND kind='FeishuMessage' GROUP BY state""",
                    (worker.store.namespace,),
                ).fetchall()
                recent = db.execute(
                    """SELECT state,created_at,updated_at FROM feishu_tasks WHERE namespace=?
                    AND kind='FeishuMessage' AND state IN ('succeeded','failed','needs_review')
                    AND updated_at >= ?""",
                    (
                        worker.store.namespace,
                        datetime.fromtimestamp(time.time() - 3600, tz=UTC).isoformat(),
                    ),
                ).fetchall()
                failures = db.execute(
                    """SELECT CASE
                    WHEN state='needs_review' THEN 'unknown_execution'
                    WHEN state='blocked' THEN 'prior_task_unresolved'
                    WHEN reply IS NOT NULL THEN 'delivery'
                    WHEN error LIKE '请求未发送或被拒绝%' THEN 'backend_unavailable_or_rejected'
                    ELSE 'other' END, count(*) FROM feishu_tasks
                    WHERE namespace=? AND kind='FeishuMessage'
                    AND state IN ('failed','needs_review','blocked') GROUP BY 1""",
                    (worker.store.namespace,),
                ).fetchall()
        except (sqlite3.Error, OSError):
            available.add_metric([], 0)
            yield available
            return
        available.add_metric([], 1)
        yield available
        counts = {row[0]: row[1] for row in rows}
        states = GaugeMetricFamily(
            "supportpilot_tasks",
            "Current persisted question tasks, including historical terminals.",
            labels=["state"],
        )
        for state in STATES:
            states.add_metric([state], counts.get(state, 0))
        yield states
        reasons = GaugeMetricFamily(
            "supportpilot_task_failures",
            "Current failed/unresolved tasks by bounded category.",
            labels=["reason"],
        )
        counts_by_reason = dict(failures)
        for reason in (
            "unknown_execution",
            "prior_task_unresolved",
            "delivery",
            "backend_unavailable_or_rejected",
            "other",
        ):
            reasons.add_metric([reason], counts_by_reason.get(reason, 0))
        yield reasons
        age = GaugeMetricFamily(
            "supportpilot_oldest_active_task_age_seconds",
            "Age of oldest queued/invoking/ready task.",
        )
        active = [
            datetime.fromisoformat(row[2]).timestamp()
            for row in rows
            if row[0] in ("queued", "invoking", "ready")
        ]
        age.add_metric([], max(0, time.time() - min(active)) if active else 0)
        yield age
        window = GaugeMetricFamily(
            "supportpilot_tasks_last_hour",
            "Current outcomes updated within one hour; not a counter.",
            labels=["state"],
        )
        for state in ("succeeded", "failed", "needs_review"):
            window.add_metric([state], sum(row[0] == state for row in recent))
        yield window
        latency = GaugeMetricFamily(
            "supportpilot_task_average_duration_last_hour_seconds",
            "Mean creation-to-final-update time of successful tasks updated within one hour.",
        )
        durations = [
            max(
                0, (datetime.fromisoformat(row[2]) - datetime.fromisoformat(row[1])).total_seconds()
            )
            for row in recent
            if row[0] == "succeeded"
        ]
        # No completions means no data, not a fabricated zero-second response time.
        if durations:
            latency.add_metric([], sum(durations) / len(durations))
        yield latency


def start_task_metrics(worker, host: str, port: int):
    collector = TaskCollector(worker)
    REGISTRY.register(collector)
    try:
        server, thread = start_http_server(port, addr=host)
    except BaseException:
        REGISTRY.unregister(collector)
        raise

    def stop():
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        REGISTRY.unregister(collector)

    return stop
