"""Exercise production wiring and real SDK form callbacks without network calls."""

import importlib.util
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(importlib.util.find_spec("lark_oapi") is None, reason="需要可选 feishu 依赖组")
@pytest.mark.parametrize("queue_backend", ["memory", "redis"])
def test_real_sdk_answer_form_and_feedback_survive_process_exit(tmp_path, queue_backend):
    redis_url = os.getenv("SUPPORTPILOT_TEST_REDIS_URL")
    if queue_backend == "redis" and not redis_url:
        pytest.skip("需要真实 Redis 才能验证 SDK 队列接入")
    script = r"""
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
try:
    import httpx
    import lark_oapi as lark
    import lark_oapi.api.im.v1 as im
    sys.path.insert(0, "src")
    import run_feishu as bridge
    from feedback import FeedbackStore
    from feishu_tasks import RedisTaskStream

    directory = Path(sys.argv[1])
    settings = bridge.FeishuSettings(
        _env_file=None, FEISHU_APP_ID="app", FEISHU_APP_SECRET="fake", AUTH_SECRET=None,
        FEISHU_SESSION_DB_PATH=directory / "sessions.sqlite3",
        FEISHU_FEEDBACK_DB_PATH=directory / "feedback.sqlite3", FEISHU_REPLY_MODE="agent",
        FEISHU_EVAL_ENABLED=False,
        FEISHU_QUEUE_BACKEND=sys.argv[2], FEISHU_REDIS_URL=sys.argv[3],
        FEISHU_TASK_DB_PATH=directory / "tasks.sqlite3",
    )
    original_connect = RedisTaskStream.connect
    RedisTaskStream.connect = lambda url, namespace, ttl: original_connect(url, namespace + str(directory), ttl)
    cards, updates, handlers, requests = [], [], [], []

    def post(url, **kwargs):
        assert url.endswith("/dev-issue-agent/invoke")
        requests.append(kwargs["json"])
        return httpx.Response(200, request=httpx.Request("POST", url),
            json={"type": "ai", "content": "合成 SDK 回答", "run_id": "sdk-run"})

    httpx.post = post

    def reply(request):
        assert request.message_id == "incoming"
        assert request.request_body.msg_type == "interactive"
        card = json.loads(request.request_body.content)
        assert card["schema"] == "2.0" and card["config"]["update_multi"] is True
        if settings.FEISHU_QUEUE_BACKEND == "memory":
            snapshot_id = card["body"]["elements"][-1]["elements"][-1]["behaviors"][0]["value"]["snapshot_id"]
            saved = FeedbackStore(settings.FEISHU_FEEDBACK_DB_PATH, "app", bridge.AGENT_ID).get_answer(snapshot_id)
            assert saved.answer == "合成 SDK 回答" and saved.run_id == "sdk-run"
            assert saved.card_message_id is None
        else:
            assert all(element["tag"] != "form" for element in card["body"]["elements"])
        cards.append(card)
        return SimpleNamespace(success=lambda: True,
            data=SimpleNamespace(message_id="answer-card", chat_id="chat"))

    def patch(request):
        card = json.loads(request.request_body.content)
        if card["body"]["elements"][-1]["tag"] == "form":
            snapshot_id = card["body"]["elements"][-1]["elements"][-1]["behaviors"][0]["value"]["snapshot_id"]
            saved = FeedbackStore(settings.FEISHU_FEEDBACK_DB_PATH, "app", bridge.AGENT_ID).get_answer(snapshot_id)
            assert saved.card_message_id == "answer-card" and saved.run_id == "sdk-run"
        updates.append(card)
        return SimpleNamespace(success=lambda: True)

    def unexpected_create(request):
        raise AssertionError("成功路径不应额外发送消息")

    api = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(
        reply=reply, patch=patch, create=unexpected_create))))
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

    def dispatch(dispatcher, event_type, event_id, event):
        payload = {"schema": "2.0", "header": {
            "event_type": event_type, "event_id": event_id, "app_id": "app",
            "tenant_key": "tenant", "create_time": "1"}, "event": event}
        result = dispatcher._do_without_validation(json.dumps(payload).encode())
        if handlers[0].tasks:
            deadline = time.monotonic() + 10
            while handlers[0].tasks.store.active():
                assert time.monotonic() < deadline, "worker did not finish"
                time.sleep(0.02)
        else:
            handlers[0]._queue.join()
        return result

    class Connection:
        def __init__(self, *args, event_handler, **kwargs):
            self.dispatcher = event_handler

        def start(self):
            message = {"sender": {"sender_type": "user", "sender_id": {"open_id": "user"}},
                "message": {"message_id": "incoming", "chat_id": "chat", "chat_type": "p2p",
                    "message_type": "text", "content": '{"text":"问题"}'}}
            dispatch(self.dispatcher, "im.message.receive_v1", "message", message)
            answer = updates[-1] if settings.FEISHU_QUEUE_BACKEND == "redis" else cards[0]
            form = answer["body"]["elements"][-1]
            value = form["elements"][-1]["behaviors"][0]["value"]
            event = {"operator": {"open_id": "user"},
                "context": {"open_chat_id": "chat", "open_message_id": "answer-card"},
                "action": {"tag": "button", "name": "submit_not_helpful", "value": value,
                    "form_value": {"comment": "请补充出处"}}}
            response = dispatch(self.dispatcher, "card.action.trigger", "feedback-1", event)
            assert json.loads(lark.JSON.marshal(response))["toast"]["type"] == "info"
            dispatch(self.dispatcher, "card.action.trigger", "feedback-1", event)
            event["action"]["value"]["rating"] = "helpful"
            event["action"]["form_value"]["comment"] = "改主意"
            dispatch(self.dispatcher, "card.action.trigger", "feedback-2", event)
            dispatch(self.dispatcher, "im.message.receive_v1", "message", message)

        async def _disconnect(self):
            pass

    lark.ws.Client = Connection
    bridge._serve_feishu(settings, lark, im)
    feedback_updates = [card for card in updates if "反馈已保存：没帮助" in json.dumps(card, ensure_ascii=False)]
    assert len(requests) == 1 and len(cards) == 1 and len(feedback_updates) == 2
    assert feedback_updates[0] == feedback_updates[1]
    assert updates[-1] == feedback_updates[-1]
    assert all(element["tag"] != "form" for element in updates[-1]["body"]["elements"])
    if settings.FEISHU_QUEUE_BACKEND == "redis":
        task = next(t for t in handlers[0].tasks.store.recent() if t.kind == "FeishuMessage")
        assert task.context["thread_id"] == requests[0]["thread_id"]
        assert task.state == "succeeded"
        notice = handlers[0].tasks.store.get_progress(task.task_id)
        assert notice.card_message_id == "answer-card" and notice.version == notice.delivered_version
        stream = handlers[0].tasks.stream
        keys = list(stream.client.scan_iter(match=f"{stream.stream}*"))
        if keys:
            stream.client.delete(*keys)
        stream.close()
    print("real_sdk_feedback=OK; network_calls=0")
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
    assert "real_sdk_feedback=OK; network_calls=0" in result.stdout
    with sqlite3.connect(tmp_path / "feedback.sqlite3") as db:
        row = db.execute("""
            SELECT s.question, s.answer, s.run_id, s.open_id, s.card_message_id,
                   f.rating, f.comment
            FROM answer_snapshots s JOIN answer_feedback f USING(snapshot_id)
        """).fetchone()
    assert row == (
        "问题",
        "合成 SDK 回答",
        "sdk-run",
        "user",
        "answer-card",
        "not_helpful",
        "请补充出处",
    )
