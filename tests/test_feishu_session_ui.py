"""飞书菜单与卡片的离线集成测试：假 API、真实 SQL 和事件处理队列。

验证操作不交给模型、当前线程路由、回调快速返回及转发/伪造操作的隔离。
"""

import json
from threading import Event
from unittest.mock import Mock

import httpx
import pytest

from feishu_cards import history_card, session_card
from feishu_sessions import FeishuSessionStore, legacy_thread_id
from run_feishu import (
    AGENT_ID,
    FeishuAction,
    FeishuHandler,
    FeishuMessage,
    FeishuSessionUI,
    FeishuSettings,
    create_agent_reply,
)


def menu_event(action="session_new", event_id="menu-1", open_id="user"):
    return {
        "schema": "2.0",
        "header": {"event_id": event_id, "event_type": "application.bot.menu_v6"},
        "event": {"event_key": action, "operator": {"operator_id": {"open_id": open_id}}},
    }


def card_event(
    action="session_current", event_id="card-1", chat_id="chat", open_id="user", target=""
):
    return {
        "schema": "2.0",
        "header": {"event_id": event_id, "event_type": "card.action.trigger"},
        "event": {
            "operator": {"open_id": open_id},
            "context": {"open_chat_id": chat_id},
            "action": {"value": {"action": action, "chat_id": chat_id, "thread_id": target}},
        },
    }


@pytest.fixture
def ui(tmp_path):
    store = FeishuSessionStore(tmp_path / "sessions.sqlite3", "app", AGENT_ID)
    sender = Mock(return_value=("loading-message", "chat"))
    updater = Mock()
    return FeishuSessionUI(store, sender, updater)


def test_first_menu_resolves_chat_and_replaces_loading_card(ui):
    ui.handle(FeishuAction("new", "user", "session_new"))
    assert ui.store.chat_for_user("user") == "chat"
    assert ui.store.current("chat").thread_id != legacy_thread_id("app", AGENT_ID, "chat")
    assert len(ui.store.recent("chat")) == 2
    ui.send_card.assert_called_once()
    assert ui.send_card.call_args.args[:2] == ("open_id", "user")
    assert ui.update_card.call_args.args[0] == "loading-message"
    assert ui.update_card.call_args.args[1]["header"]["title"]["content"] == "会话已就绪"


@pytest.mark.parametrize("action", ["session_current", "session_history", "session_new"])
def test_known_user_menu_returns_card_without_model(ui, action):
    ui.store.current("chat", "user")
    ui.handle(FeishuAction("known-event", "user", action))
    ui.update_card.assert_not_called()
    card = ui.send_card.call_args.args[2]
    assert card["schema"] == "2.0"
    assert card["header"]["title"]["content"] in {"当前会话", "历史会话", "会话已就绪"}


def test_old_card_cannot_switch_another_users_thread(ui):
    owned = ui.store.current("chat", "user")
    other = ui.store.current("other-chat", "other-user")
    ui.handle(FeishuAction("bad", "user", "session_switch", "chat", other.thread_id))
    assert ui.store.current("chat").thread_id == owned.thread_id
    assert ui.send_card.call_args.args[2]["header"]["title"]["content"] == "操作未完成"


def test_forwarded_card_is_rejected_without_disclosing_foreign_history(ui):
    original = ui.store.current("chat", "user")
    ui.handle(FeishuAction("forwarded", "user", "session_new", "foreign-group"))
    assert ui.store.current("chat").thread_id == original.thread_id
    assert ui.send_card.call_args.args[:2] == ("open_id", "user")
    assert len(ui.store.recent("chat")) == 1


def test_card_callback_returns_before_work_finishes(ui):
    entered, release = Event(), Event()

    def slow_action(_):
        entered.set()
        release.wait(timeout=2)

    ui.handle = slow_action
    handler = FeishuHandler(Mock(), session_ui=ui)
    handler.start()
    try:
        response = handler.on_card_action(card_event())
        assert response["toast"]["type"] == "info"
        assert entered.wait(timeout=1)
        assert not release.is_set()
    finally:
        release.set()
        handler.stop()


def test_duplicate_menu_and_card_events_are_enqueued_once(ui):
    ui.handle = Mock()
    handler = FeishuHandler(Mock(), session_ui=ui)
    handler.on_menu_event(menu_event())
    handler.on_menu_event(menu_event())
    handler.on_card_action(card_event())
    handler.on_card_action(card_event())
    assert handler._queue.qsize() == 2


@pytest.mark.parametrize(
    "change", ["foreign_chat", "no_user", "no_id", "bad_value", "unknown_action"]
)
def test_invalid_card_callback_is_rejected(ui, change):
    event = card_event()
    if change == "foreign_chat":
        event["event"]["context"]["open_chat_id"] = "different"
    elif change == "no_user":
        event["event"]["operator"] = {}
    elif change == "no_id":
        event["header"] = {}
    elif change == "bad_value":
        event["event"]["action"]["value"] = "not-an-object"
    else:
        event["event"]["action"]["value"]["action"] = "delete-everything"
    handler = FeishuHandler(Mock(), session_ui=ui)
    assert handler.on_card_action(event)["toast"]["type"] == "error"
    assert handler._queue.empty()


def test_normal_messages_and_menu_actions_share_processing_order(ui):
    ui.store.current("chat", "user")
    order = []
    ui.handle = lambda action: order.append(action.action)
    handler = FeishuHandler(lambda *_: order.append("reply"), lambda _: "response", ui)
    handler.on_event(
        {
            "event": {
                "sender": {"sender_type": "user", "sender_id": {"open_id": "user"}},
                "message": {
                    "message_id": "msg",
                    "chat_id": "chat",
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": '{"text":"问题"}',
                },
            }
        }
    )
    handler.on_menu_event(menu_event())
    handler.start()
    try:
        handler._queue.join()
        assert order == ["reply", "session_new"]
    finally:
        handler.stop()


def test_slash_command_uses_real_session_action_not_llm(ui):
    generate = Mock()
    handler = FeishuHandler(Mock(), generate, ui)
    handler.on_event(
        {
            "event": {
                "sender": {"sender_type": "user", "sender_id": {"open_id": "user"}},
                "message": {
                    "message_id": "msg",
                    "chat_id": "chat",
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": '{"text":"/new"}',
                },
            }
        }
    )
    item = handler._queue.get_nowait()
    assert isinstance(item, FeishuAction)
    assert item.action == "session_new"
    generate.assert_not_called()


def test_agent_routing_changes_and_restores_without_changing_user_id(tmp_path, monkeypatch):
    settings = FeishuSettings(_env_file=None, FEISHU_APP_ID="app", FEISHU_APP_SECRET="fake")
    store = FeishuSessionStore(tmp_path / "sessions.sqlite3", "app", AGENT_ID)
    requests, histories = [], {}

    def post(url, **kwargs):
        body = kwargs["json"]
        requests.append(body)
        history = histories.setdefault(body["thread_id"], [])
        history.append(body["message"])
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"type": "ai", "content": " / ".join(history)},
        )

    monkeypatch.setattr(httpx, "post", post)
    reply = create_agent_reply(settings, store)
    assert reply(FeishuMessage("m1", "chat", "跨域", "user")) == "跨域"
    old = requests[-1]["thread_id"]
    new = store.change("chat", "new")
    assert reply(FeishuMessage("m2", "chat", "空指针", "user")) == "空指针"
    assert requests[-1]["thread_id"] == new.thread_id != old
    reopened = FeishuSessionStore(store.path, "app", AGENT_ID)
    restarted = create_agent_reply(settings, reopened)
    assert restarted(FeishuMessage("m3", "chat", "继续", "user")) == "空指针 / 继续"
    reopened.change("chat", "resume-old", old)
    assert restarted(FeishuMessage("m4", "chat", "恢复旧问题", "user")) == "跨域 / 恢复旧问题"
    assert len({body["user_id"] for body in requests}) == 1


def test_cards_use_plain_titles_and_scope_button_values(ui):
    item = ui.store.change("chat", "new")
    ui.store.record_turn("chat", item.thread_id, "[标题](https://example.com) **不解析**")
    current = ui.store.current("chat")
    card = session_card(current, "chat")
    assert card["header"]["subtitle"] == {"tag": "plain_text", "content": current.title}
    history = history_card(ui.store.recent("chat"), current.thread_id, "chat")
    button = history["body"]["elements"][1]
    assert button["text"]["tag"] == "plain_text"
    assert button["behaviors"][0]["value"] == {
        "action": "session_switch",
        "chat_id": "chat",
        "thread_id": current.thread_id,
    }
    assert json.loads(json.dumps(history, ensure_ascii=False)) == history
