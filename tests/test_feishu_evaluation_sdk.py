"""Real SDK menu dispatch through background evaluation, publication and notification."""

import importlib.util
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(importlib.util.find_spec("lark_oapi") is None, reason="需要可选 feishu 依赖组")
@pytest.mark.parametrize("queue_backend", ["memory", "redis"])
def test_real_sdk_menu_background_report_and_completion(tmp_path, queue_backend):
    redis_url = os.getenv("SUPPORTPILOT_TEST_REDIS_URL")
    if queue_backend == "redis" and not redis_url:
        pytest.skip("需要真实 Redis 才能验证启动时恢复评测菜单任务")
    script = r"""
import asyncio
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
try:
    import httpx
    import requests
    import lark_oapi as lark
    import lark_oapi.api.im.v1 as im
    sys.path.insert(0, "src")
    import run_feishu as bridge
    from evaluation import Case, Suite, Turn
    from evaluation_jobs import EvaluationJobStore
    from feishu_tasks import MessageTaskStore, RedisTaskStream
    from feishu_evaluation import EvaluationAction

    root = Path(sys.argv[1])
    cases = root / "cases.json"
    suite = Suite(id="sdk", version="1", description="合成案例", cases=[
        Case(id="one", title="案例", category="test", turns=[Turn(message="你好", rubric=["人工检查"])])])
    cases.write_text(suite.model_dump_json(), encoding="utf-8")
    settings = bridge.FeishuSettings(
        _env_file=None, FEISHU_APP_ID="app", FEISHU_APP_SECRET="fake", AUTH_SECRET=None,
        FEISHU_SESSION_DB_PATH=root / "sessions.sqlite3",
        FEISHU_FEEDBACK_DB_PATH=root / "feedback.sqlite3", FEISHU_REPLY_MODE="agent",
        FEISHU_EVAL_ENABLED=True, FEISHU_EVAL_DB_PATH=root / "jobs.sqlite3",
        FEISHU_EVAL_CASES_PATH=cases, FEISHU_EVAL_OUTPUT_ROOT=root / "runs",
        FEISHU_QUEUE_BACKEND=sys.argv[2], FEISHU_REDIS_URL=sys.argv[3],
        FEISHU_TASK_DB_PATH=root / "tasks.sqlite3",
    )
    original_connect = RedisTaskStream.connect
    RedisTaskStream.connect = lambda url, namespace, ttl: original_connect(url, namespace + str(root), ttl)
    if settings.FEISHU_QUEUE_BACKEND == "redis":
        task_store = MessageTaskStore(settings.FEISHU_TASK_DB_PATH, json.dumps(["app", bridge.AGENT_ID]))
        task_store.enqueue("action:start", "EvaluationAction",
            asdict(EvaluationAction("start", "owner", "eval_start")), 32)
    cards, handlers, questions, permissions = [], [], [], []
    completed = Event()

    def info(url, **kwargs):
        return httpx.Response(200, request=httpx.Request("GET", url), json={
            "agents": [{"key": "dev-issue-agent", "description": "test"}],
            "models": ["fake"], "default_agent": "dev-issue-agent", "default_model": "fake"})
    httpx.get = info

    class Stream:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def raise_for_status(self):
            pass
        async def aiter_lines(self):
            yield "data: " + json.dumps({"type": "message", "content": {
                "type": "ai", "content": "合成问答", "run_id": "sdk-run"}})
            yield "data: [DONE]"

    class BackendClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def stream(self, method, url, **kwargs):
            assert url.endswith("/dev-issue-agent/stream")
            questions.append(kwargs["json"])
            return Stream()
    httpx.AsyncClient = BackendClient

    class PublisherTransport:
        requests = requests
        def __init__(self, settings):
            pass
        def request(self, method, url, **kwargs):
            data = {}
            if url.endswith("/tenant_access_token/internal"):
                payload = {"code": 0, "tenant_access_token": "fake-token"}
            else:
                if url.endswith("/docx/v1/documents"):
                    data = {"document": {"document_id": "doc123"}}
                elif url.endswith("/children"):
                    data = {"children": [{} for _ in kwargs["json"]["children"]]}
                elif url.endswith("/members") and method == "POST":
                    permissions.append(kwargs["json"])
                elif url.endswith("/members"):
                    data = {"items": [{"member_type": "openid", "member_id": "owner", "perm": "view"}]}
                elif url.endswith("/batch_query"):
                    data = {"metas": [{"doc_token": "doc123", "url": "https://tenant.feishu.cn/docx/doc123"}]}
                payload = {"code": 0, "data": data}
            return SimpleNamespace(status_code=200, json=lambda: payload)
    bridge.FeishuHTTPTransport = PublisherTransport

    def create(request):
        card = json.loads(request.request_body.content)
        cards.append(card)
        if card["header"]["title"]["content"] == "评测完成，报告已发布":
            completed.set()
        return SimpleNamespace(success=lambda: True,
            data=SimpleNamespace(message_id=f"card-{len(cards)}", chat_id="chat"))
    api = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(
        create=create, reply=Mock(), patch=Mock()))))
    builder = Mock()
    for method in ("app_id", "app_secret", "timeout", "log_level"):
        getattr(builder, method).return_value = builder
    builder.build.return_value = api
    lark.Client = SimpleNamespace(builder=lambda: builder)
    OriginalHandler = bridge.FeishuHandler
    class TrackingHandler(OriginalHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            handlers.append(self)
    bridge.FeishuHandler = TrackingHandler

    def dispatch(dispatcher, kind, event_id, event):
        payload = {"schema": "2.0", "header": {"event_type": kind, "event_id": event_id,
            "app_id": "app", "tenant_key": "tenant", "create_time": "1"}, "event": event}
        result = dispatcher._do_without_validation(json.dumps(payload).encode())
        if handlers[0].tasks:
            deadline = time.monotonic() + 10
            while handlers[0].tasks.store.active():
                assert time.monotonic() < deadline
                time.sleep(0.02)
        else:
            handlers[0]._queue.join()
        return result

    class Connection:
        def __init__(self, *args, event_handler, **kwargs):
            self.dispatcher = event_handler
        def start(self):
            event = {"event_key": "eval_start", "operator": {"operator_id": {"open_id": "owner"}}}
            dispatch(self.dispatcher, "application.bot.menu_v6", "start", event)
            dispatch(self.dispatcher, "application.bot.menu_v6", "start", event)
            assert completed.wait(5)
            store = EvaluationJobStore(settings.FEISHU_EVAL_DB_PATH, "app", bridge.AGENT_ID)
            job = store.latest("owner")
            response = dispatch(self.dispatcher, "card.action.trigger", "status", {
                "operator": {"open_id": "owner"}, "context": {"open_message_id": "card-1", "open_chat_id": "chat"},
                "action": {"tag": "button", "value": {"action": "eval_status", "job_id": job.job_id}}})
            assert json.loads(lark.JSON.marshal(response))["toast"]["type"] == "info"
        async def _disconnect(self):
            pass
    lark.ws.Client = Connection
    bridge._serve_feishu(settings, lark, im)
    assert len(questions) == 1 and questions[0]["stream_tokens"] is False
    assert permissions == [{"member_type": "openid", "member_id": "owner", "perm": "view", "type": "user"}]
    assert any("https://tenant.feishu.cn/docx/doc123" in json.dumps(card) for card in cards)
    if handlers[0].tasks:
        stream = handlers[0].tasks.stream
        keys = list(stream.client.scan_iter(match=f"{stream.stream}*"))
        if keys:
            stream.client.delete(*keys)
        stream.close()
    print("real_sdk_evaluation=OK; network_calls=0")
finally:
    pending = asyncio.all_tasks(loop)
    for task in pending:
        task.cancel()
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.run_until_complete(loop.shutdown_default_executor())
    loop.close()
    asyncio.set_event_loop(None)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path),
            queue_backend,
            redis_url or "redis://127.0.0.1:6379/15",
        ],
        cwd=Path(__file__).parents[1],
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        encoding="utf-8",
        capture_output=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "real_sdk_evaluation=OK; network_calls=0" in result.stdout
    with sqlite3.connect(tmp_path / "jobs.sqlite3") as db:
        rows = db.execute(
            "SELECT status, report_url, document_id, notified_at, output_dir FROM evaluation_jobs"
        ).fetchall()
    assert len(rows) == 1
    state, url, document, notified, output = rows[0]
    assert state == "succeeded" and notified
    assert url.endswith("/docx/doc123") and document == "doc123"
    assert (Path(output) / "results.jsonl").is_file()
    assert (Path(output) / "report.md").is_file()
