"""Check configured Cloud credentials; optionally upload and read back a synthetic trace.

Never invokes a paid model, reads private documents, or prints credentials.
"""

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from langchain_core.runnables import RunnableLambda  # noqa: E402

from core import settings  # noqa: E402
from observability.identifiers import task_id_for, trace_id_for  # noqa: E402
from observability.tracing import (  # noqa: E402
    get_tracing_client,
    shutdown_tracing,
    trace_metadata,
    tracing_callback,
)


def verify_trace(client, trace_id, options):
    last_error = last_status = None
    for attempt in range(4):
        try:
            result = client.api.observations.get_many(
                trace_id=trace_id, fields="core,basic", limit=5, request_options=options
            )
            if any(item.trace_id == trace_id for item in result.data):
                print(json.dumps({"status": "verified", "trace_id": trace_id}))
                return 0
        except Exception as exc:
            last_error = type(exc).__name__
            last_status = getattr(exc, "status_code", None)
            if last_status in {401, 403, 410, 429}:
                break
        if attempt < 3:
            time.sleep(3)
    print(
        json.dumps(
            {
                "status": "not_verified",
                "trace_id": trace_id,
                "error_type": last_error,
                "http_status": last_status,
            }
        )
    )
    return 1


def main():
    parser = argparse.ArgumentParser(description="核对 Langfuse 配置；不调用模型或飞书")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--send-test", action="store_true", help="上传并读回一条标记为 synthetic 的测试 trace"
    )
    modes.add_argument("--trace-id", help="只查询已有 trace，不上传新记录")
    args = parser.parse_args()
    if args.trace_id and not re.fullmatch(r"[0-9a-f]{32}", args.trace_id):
        parser.error("trace-id 应为 32 位小写十六进制")
    # SDK error responses must not accidentally print credentials or project data.
    logging.getLogger("langfuse").setLevel(logging.CRITICAL)
    client = get_tracing_client(settings)
    if client is None:
        print(
            json.dumps(
                {"status": "unavailable", "hint": "检查 LANGFUSE_TRACING 和项目密钥配置"},
                ensure_ascii=False,
            )
        )
        return 2
    options = {"timeout_in_seconds": settings.LANGFUSE_TIMEOUT, "max_retries": 0}
    try:
        projects = client.api.projects.get(request_options=options)
        if not projects.data:
            print(json.dumps({"status": "no_accessible_project"}))
            return 2
        if args.trace_id:
            return verify_trace(client, args.trace_id, options)
        if not args.send_test:
            print(json.dumps({"status": "authenticated", "trace_sent": False}))
            return 0
        run_id = uuid4()
        task_id = task_id_for("supportpilot-synthetic-check", str(run_id))
        trace_id = trace_id_for(task_id, run_id)
        handler = tracing_callback(settings, trace_id)
        if handler is None:
            print(json.dumps({"status": "callback_unavailable"}))
            return 2
        metadata = trace_metadata(
            "connectivity-check", f"check-{run_id}", "synthetic", run_id, trace_id, task_id
        )
        metadata["synthetic"] = True
        RunnableLambda(lambda _: "Synthetic connectivity check; no model called.").invoke(
            "Synthetic connectivity check",
            config={
                "run_id": run_id,
                "run_name": "supportpilot-connectivity-check",
                "callbacks": [handler],
                "metadata": metadata,
            },
        )
        # Explicit flush is appropriate for a one-shot CLI, never for each online request.
        client.flush()
        return verify_trace(client, trace_id, options)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        return 1
    finally:
        shutdown_tracing()


if __name__ == "__main__":
    raise SystemExit(main())
