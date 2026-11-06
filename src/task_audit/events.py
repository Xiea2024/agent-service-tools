"""Append events in the caller's transaction; emit content-free logs only after commit."""

import json
import logging
import re
from datetime import UTC, datetime, timedelta, timezone

from observability.identifiers import trace_id_for

logger = logging.getLogger(__name__)
DETAIL_KEYS = {
    "snapshot_id",
    "version",
    "target_state",
    "card_message_id",
    "retry_at",
    "action",
    "operation_id",
    "observed_state",
}


def error_category(state, reply, error):
    if not error:
        return None
    if state == "needs_review":
        return "unknown_execution"
    if state == "blocked":
        return "prior_task_unresolved"
    if reply:
        return "delivery"
    if str(error).startswith("请求未发送或被拒绝"):
        return "backend_unavailable_or_rejected"
    return "operation_error"


def append_event(
    db,
    namespace,
    task_id,
    event_type,
    *,
    from_state=None,
    to_state=None,
    attempt=0,
    error_code=None,
    details=None,
    actor=None,
    reason=None,
    dedupe_key=None,
):
    if set(details or {}) - DETAIL_KEYS:
        raise ValueError("事件详情只能包含关联编号和阶段元数据，不允许正文或任意配置")
    row = db.execute(
        "SELECT context FROM feishu_tasks WHERE namespace=? AND task_id=?", (namespace, task_id)
    ).fetchone()
    if row is None:
        raise ValueError("任务不存在或不属于当前应用")
    context = json.loads(row[0] or "{}")
    # Explicit whitelist: never copy payload, reply, exception text or arbitrary config.
    data = {
        key: value for key, value in context.items() if key in {"thread_id", "trace_id", "run_id"}
    }
    data.setdefault("trace_id", trace_id_for(task_id))
    data.update(details or {})
    occurred_at = datetime.now(UTC).isoformat()
    cursor = db.execute(
        """INSERT OR IGNORE INTO task_events
        (namespace,task_id,event_type,occurred_at,from_state,to_state,attempt,error_code,
         details,actor,reason,dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            namespace,
            task_id,
            event_type,
            occurred_at,
            from_state,
            to_state,
            attempt,
            error_code,
            json.dumps(data, ensure_ascii=False),
            actor,
            reason,
            dedupe_key,
        ),
    )
    if cursor.rowcount == 0:
        return None
    return {
        "event_id": cursor.lastrowid,
        "task_id": task_id,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "from_state": from_state,
        "to_state": to_state,
        "attempt": attempt,
        "error_code": error_code,
        **data,
    }


def emit_event(event):
    if event:
        # Operator reason/identity remain in the local audit table, not telemetry logs.
        logger.info(json.dumps(event, ensure_ascii=False))


def normalize_time(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    # Human-facing CLI dates follow the project's Asia/Shanghai convention.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return parsed.astimezone(UTC).isoformat()


def validate_recovery(action, actor, reason, operation_id):
    if action not in {"abandon", "retry-send"}:
        raise ValueError("不支持的恢复操作")
    if not isinstance(actor, str) or not 1 <= len(actor.strip()) <= 80:
        raise ValueError("操作人不能为空且最多 80 字")
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000:
        raise ValueError("恢复操作需要原因，最多 1000 字，请勿填写密钥或私人正文")
    if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", operation_id):
        raise ValueError("操作编号需为 1–80 位字母、数字、下划线或连字符")
