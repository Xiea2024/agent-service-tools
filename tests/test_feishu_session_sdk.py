"""真实飞书 SDK 的离线协议检查：验证事件分派、消息构造与卡片回调响应。

SDK 导入会持有事件循环，因此在独立子进程中测试并清理任务；所有 API 与
WebSocket 都被替换，不发送消息、不建立连接、不读取真实凭据。
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(importlib.util.find_spec("lark_oapi") is None, reason="需要可选 feishu 依赖组")
def test_real_sdk_dispatches_session_menu_and_card_callbacks(tmp_path):
    script = r"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
try:
    import lark_oapi as lark
    import lark_oapi.api.im.v1 as im
    sys.path.insert(0, "src")
    import run_feishu as bridge
    from feishu_sessions import FeishuSessionStore, legacy_thread_id

    path = Path(sys.argv[1]) / "sessions.sqlite3"
    settings = bridge.FeishuSettings(_env_file=None, FEISHU_APP_ID="app",
        FEISHU_APP_SECRET="fake", FEISHU_SESSION_DB_PATH=path, FEISHU_REPLY_MODE="echo")
    cards, patches, replies, handlers = [], [], [], []

    def create(request):
        assert request.request_body.msg_type == "interactive"
        card = json.loads(request.request_body.content)
        assert card["schema"] == "2.0"
        assert card["config"]["update_multi"] is True
        cards.append(card)
        return SimpleNamespace(success=lambda: True,
            data=SimpleNamespace(message_id=f"out-{len(cards)}", chat_id="chat"))

    def patch(request):
        patches.append(json.loads(request.request_body.content))
        return SimpleNamespace(success=lambda: True)

    def reply(request):
        replies.append(request.request_body)
        return SimpleNamespace(success=lambda: True)

    api = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(
        create=create, patch=patch, reply=reply))))
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
        payload = {"schema": "2.0", "header": {"event_type": event_type,
            "event_id": event_id, "app_id": "app", "tenant_key": "tenant", "create_time": "1"},
            "event": event}
        response = dispatcher._do_without_validation(json.dumps(payload).encode())
        handlers[0]._queue.join()
        return response

    class Connection:
        def __init__(self, *args, event_handler, **kwargs):
            self.dispatcher = event_handler
        def start(self):
            for key in ("session_current", "session_new", "session_history"):
                dispatch(self.dispatcher, "application.bot.menu_v6", key,
                    {"event_key": key, "operator": {"operator_id": {"open_id": "user"}}})
            store = FeishuSessionStore(path, "app", bridge.AGENT_ID)
            old = legacy_thread_id("app", bridge.AGENT_ID, "chat")
            assert store.current("chat").thread_id != old
            response = dispatch(self.dispatcher, "card.action.trigger", "switch",
                {"operator": {"open_id": "user"},
                 "context": {"open_chat_id": "chat", "open_message_id": "out-3"},
                 "action": {"tag": "button", "value": {
                     "action": "session_switch", "chat_id": "chat", "thread_id": old}}})
            assert json.loads(lark.JSON.marshal(response))["toast"]["type"] == "info"
            assert store.current("chat").thread_id == old
            dispatch(self.dispatcher, "im.message.receive_v1", "message",
                {"sender": {"sender_type": "user", "sender_id": {"open_id": "user"}},
                 "message": {"message_id": "incoming", "chat_id": "chat", "chat_type": "p2p",
                     "message_type": "text", "content": '{"text":"你好"}'}})
        async def _disconnect(self):
            pass

    lark.ws.Client = Connection
    bridge._serve_feishu(settings, lark, im)
    assert len(cards) == 4 and len(patches) == 1 and len(replies) == 1
    assert patches[0]["header"]["title"]["content"] == "当前会话"
    assert cards[2]["header"]["title"]["content"] == "历史会话"
    print("real_sdk_session_protocol=OK; network_calls=0")
finally:
    pending = asyncio.all_tasks(loop)
    for task in pending:
        task.cancel()
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.close()
    asyncio.set_event_loop(None)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).parents[1],
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        encoding="utf-8",
        capture_output=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "real_sdk_session_protocol=OK; network_calls=0" in result.stdout
