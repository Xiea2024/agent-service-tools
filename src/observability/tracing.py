"""Optional, bounded Langfuse Cloud tracing using the installed SDK.

No per-request flush or auth check. Default content masking runs before export;
task/session IDs and provider-reported token usage remain available.
"""

import hashlib
import json
import logging
import os
import re
from threading import Lock

from langfuse import Langfuse
from langfuse.langchain import CallbackHandler

logger = logging.getLogger(__name__)
_lock = Lock()
_client = None
_attempted = False
SAFE_METADATA = {
    "task_id",
    "trace_id",
    "run_id",
    "agent_id",
    "thread_id",
    "synthetic",
    "langfuse_session_id",
    "langfuse_user_id",
    "langfuse_tags",
}


def mask_content(*, data, capture=False, **kwargs):
    """Default denies content. Opt-in text capture redacts common credential forms only."""
    if data is None:
        return None
    if isinstance(data, dict):
        if not capture:
            return {key: value for key, value in data.items() if key in SAFE_METADATA}
        return {
            key: "[REDACTED]"
            if re.search(r"secret|password|authorization|api.?key|token", str(key), re.I)
            else mask_content(data=value, capture=True)
            for key, value in data.items()
        }
    if not capture:
        return "[CONTENT OMITTED]"
    if isinstance(data, (list, tuple)):
        return [mask_content(data=item, capture=True) for item in data]
    if isinstance(data, str):
        data = re.sub(r"\b(?:sk|pk)-[A-Za-z0-9_-]+", "[REDACTED]", data)
        data = re.sub(r"(?i)Bearer\s+[^\s\"']+", "Bearer [REDACTED]", data)
        data = re.sub(
            r"(?i)((?:password|api[_-]?key|secret|token)\s*[:=]\s*)[^\s,;]+",
            r"\1[REDACTED]",
            data,
        )
        return data[:8000]
    if isinstance(data, (int, float, bool)):
        return data
    return mask_content(data=str(data), capture=True)


class SafeCallbackHandler(CallbackHandler):
    """Keep exception bodies out of error spans and mark returned tool errors as errors."""

    raise_error = False

    def on_chain_error(self, error, **kwargs):
        return super().on_chain_error(RuntimeError(type(error).__name__), **kwargs)

    def on_llm_error(self, error, **kwargs):
        return super().on_llm_error(RuntimeError(type(error).__name__), **kwargs)

    def on_tool_error(self, error, **kwargs):
        return super().on_tool_error(RuntimeError(type(error).__name__), **kwargs)

    def on_retriever_error(self, error, **kwargs):
        return super().on_retriever_error(RuntimeError(type(error).__name__), **kwargs)

    def on_tool_end(self, output, **kwargs):
        content = getattr(output, "content", output)
        try:
            payload = json.loads(content) if isinstance(content, str) else content
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict) and payload.get("status") == "error":
            return super().on_tool_error(RuntimeError("RetrievalError"), **kwargs)
        return super().on_tool_end(output, **kwargs)


def get_tracing_client(settings):
    global _client, _attempted
    if settings.LANGFUSE_TRACING is not True:
        return None
    with _lock:
        if _attempted:
            return _client
        _attempted = True
        if not settings.LANGFUSE_PUBLIC_KEY or not settings.LANGFUSE_SECRET_KEY:
            logger.warning("Langfuse disabled: missing project credentials")
            return None
        try:
            # The OpenTelemetry batch queue is bounded; no durable telemetry queue is added.
            os.environ.setdefault("OTEL_BSP_MAX_QUEUE_SIZE", "512")
            _client = Langfuse(
                public_key=settings.LANGFUSE_PUBLIC_KEY.get_secret_value(),
                secret_key=settings.LANGFUSE_SECRET_KEY.get_secret_value(),
                host=settings.LANGFUSE_HOST.rstrip("/"),
                timeout=settings.LANGFUSE_TIMEOUT,
                flush_at=32,
                flush_interval=5,
                media_upload_thread_count=1,
                mask=lambda **kw: mask_content(capture=settings.LANGFUSE_CAPTURE_CONTENT, **kw),
            )
        except Exception as exc:
            logger.warning("Langfuse initialization failed: %s", type(exc).__name__)
            _client = None
        return _client


def tracing_callback(settings, trace_id: str):
    client = get_tracing_client(settings)
    if client is None:
        return None
    try:
        return SafeCallbackHandler(
            public_key=settings.LANGFUSE_PUBLIC_KEY.get_secret_value(),
            trace_context={"trace_id": trace_id},
        )
    except Exception as exc:
        logger.warning("Langfuse callback unavailable: %s", type(exc).__name__)
        return None


def trace_metadata(agent_id, thread_id, user_id, run_id, trace_id, task_id=None):
    metadata = {
        "user_id": user_id,
        "agent_id": agent_id,
        "run_id": str(run_id),
        "trace_id": trace_id,
        "thread_id": thread_id,
        "langfuse_session_id": thread_id,
        "langfuse_user_id": hashlib.sha256(user_id.encode()).hexdigest()[:24],
        "langfuse_tags": ["supportpilot"],
    }
    if task_id:
        metadata["task_id"] = task_id
    return metadata


def tracing_status(settings):
    if settings.LANGFUSE_TRACING is not True:
        return "disabled"
    return "initialized" if _client is not None else "unavailable"


def shutdown_tracing():
    global _client, _attempted
    with _lock:
        client, _client = _client, None
        _attempted = False
    if client is not None:
        try:
            client.shutdown()
        except Exception as exc:
            logger.warning("Langfuse shutdown failed: %s", type(exc).__name__)
