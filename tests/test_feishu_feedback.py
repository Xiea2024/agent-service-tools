import json
from threading import Event
from unittest.mock import Mock

import httpx
import pytest

from feedback import FeedbackStore
from feishu_cards import answer_card
from feishu_feedback import FeishuFeedbackUI, parse_feedback_action
from feishu_sessions import FeishuSessionStore
from run_feishu import AGENT_ID, FeishuHandler, FeishuMessage, FeishuSettings, create_agent_reply


@pytest.fixture
def ui(tmp_path):
    store = FeedbackStore(tmp_path / "feedback.sqlite3", "app", AGENT_ID)
    return FeishuFeedbackUI(store, Mock(return_value=("card", "chat")), Mock(), Mock())


def snapshot(ui):
    return ui.store.save_answer(
        source_message_id="incoming",
        chat_id="chat",
        open_id="user",
        user_id="backend-user",
        thread_id="thread",
        run_id="run",
        question="问题",
        answer="完整回答",
    )


def callback(snapshot_id, **changes):
    context = dict(open_chat_id="chat", open_message_id="card")
    context.update(changes.pop("context", {}))
    event = {
        "header": {"event_id": "event"},
        "event": {
            "operator": {"open_id": "user"},
            "context": context,
            "action": {
                "tag": "button",
                "name": "submit_helpful",
                "value": {
                    "action": "feedback_submit",
                    "snapshot_id": snapshot_id,
                    "rating": "helpful",
                },
                "form_value": {"comment": "补充意见"},
            },
        },
    }
    event.update(changes)
    return event


def test_card_contains_snapshot_reference_and_optional_form(ui):
    saved = snapshot(ui)
    card = answer_card(saved)
    assert card["config"]["update_multi"] is True
    assert card["body"]["elements"][0]["content"] == "完整回答"
    form = card["body"]["elements"][-1]
    assert form["tag"] == "form" and form["name"]
    comment, yes, no = form["elements"]
    assert comment["name"] == "comment" and comment["required"] is False
    assert len({yes["name"], no["name"], comment["name"], form["name"]}) == 4
    for button, rating in ((yes, "helpful"), (no, "not_helpful")):
        assert button["form_action_type"] == "submit"
        assert button["behaviors"][0]["value"] == {
            "action": "feedback_submit",
            "snapshot_id": saved.snapshot_id,
            "rating": rating,
        }


def test_snapshot_is_durable_before_sending_and_card_binding_is_persisted(ui):
    saved = snapshot(ui)

    def reply(message_id, card):
        reopened = FeedbackStore(ui.store.path, "app", AGENT_ID)
        assert reopened.get_answer(saved.snapshot_id).answer == "完整回答"
        return "card", "chat"

    ui.reply_card.side_effect = reply
    ui.send_answer(saved)
    assert ui.store.get_answer(saved.snapshot_id).card_message_id == "card"
    ui.send_answer(saved)
    ui.reply_card.assert_called_once()


def test_submit_updates_card_only_after_persistence_and_repeated_click_is_consistent(ui):
    saved = snapshot(ui)
    ui.send_answer(saved)
    action = parse_feedback_action(callback(saved.snapshot_id))

    def update(message_id, card):
        assert ui.store.get_feedback(saved.snapshot_id).rating == "helpful"
        assert "反馈已保存" in json.dumps(card, ensure_ascii=False)
        assert not any(e["tag"] == "form" for e in card["body"]["elements"])

    ui.update_card.side_effect = update
    ui.handle(action)
    again = callback(saved.snapshot_id)
    again["header"]["event_id"] = "event-2"
    again["event"]["action"]["value"]["rating"] = "not_helpful"
    ui.handle(parse_feedback_action(again))
    assert ui.update_card.call_count == 2
    assert ui.update_card.call_args_list[0] == ui.update_card.call_args_list[1]
    assert ui.store.get_feedback(saved.snapshot_id).comment == "补充意见"


def test_foreign_card_callback_cannot_update_card_or_save_feedback(ui):
    saved = snapshot(ui)
    ui.send_answer(saved)
    data = callback(saved.snapshot_id, context={"open_message_id": "forwarded"})
    ui.handle(parse_feedback_action(data))
    assert ui.store.get_feedback(saved.snapshot_id) is None
    ui.update_card.assert_not_called()
    assert ui.send_card.call_args.args[:2] == ("open_id", "user")
    assert ui.send_card.call_args.args[2]["header"]["title"]["content"] == "反馈未保存"


@pytest.mark.parametrize("form", [{}, {"comment": ""}, {"comment": None}])
def test_comment_can_be_left_empty(ui, form):
    saved = snapshot(ui)
    ui.send_answer(saved)
    data = callback(saved.snapshot_id)
    data["event"]["action"]["form_value"] = form
    ui.handle(parse_feedback_action(data))
    assert ui.store.get_feedback(saved.snapshot_id).comment == ""


def test_card_update_failure_does_not_lose_feedback_or_claim_it_failed(ui):
    saved = snapshot(ui)
    ui.send_answer(saved)
    ui.update_card.side_effect = RuntimeError("网络错误")
    ui.handle(parse_feedback_action(callback(saved.snapshot_id)))
    assert ui.store.get_feedback(saved.snapshot_id).rating == "helpful"
    assert ui.send_card.call_args.args[2]["header"]["title"]["content"] == "反馈已保存"


@pytest.mark.parametrize(
    "change", ["no_user", "no_message", "no_snapshot", "bad_form", "long_comment", "bad_rating"]
)
def test_invalid_feedback_is_rejected_before_queueing(ui, change):
    data = callback("snapshot")
    if change == "no_user":
        data["event"]["operator"] = {}
    elif change == "no_message":
        data["event"]["context"] = {"open_chat_id": "chat"}
    elif change == "no_snapshot":
        data["event"]["action"]["value"].pop("snapshot_id")
    elif change == "bad_form":
        data["event"]["action"]["form_value"] = "text"
    elif change == "long_comment":
        data["event"]["action"]["form_value"] = {"comment": "长" * 1001}
    else:
        data["event"]["action"]["value"]["rating"] = ["helpful"]
    handler = FeishuHandler(Mock(), feedback_ui=ui)
    assert handler.on_card_action(data)["toast"]["type"] == "error"
    assert handler._queue.empty()


def test_feedback_callback_returns_before_database_worker_finishes(ui):
    entered, release = Event(), Event()

    def slow_handle(action):
        entered.set()
        release.wait(timeout=2)

    ui.handle = slow_handle
    handler = FeishuHandler(Mock(), feedback_ui=ui)
    handler.start()
    try:
        result = handler.on_card_action(callback("snapshot"))
        assert result["toast"]["type"] == "info"
        assert entered.wait(timeout=1)
        assert not release.is_set()
    finally:
        release.set()
        handler.stop()


def test_agent_reply_saves_run_and_reuses_snapshot_after_restart(ui, tmp_path, monkeypatch):
    settings = FeishuSettings(_env_file=None, FEISHU_APP_ID="app", FEISHU_APP_SECRET="fake")
    sessions = FeishuSessionStore(tmp_path / "sessions.sqlite3", "app", AGENT_ID)
    post = Mock(
        return_value=httpx.Response(
            200,
            request=httpx.Request("POST", "http://test"),
            json={"type": "ai", "content": "真实入口的合成回答", "run_id": "run-from-backend"},
        )
    )
    monkeypatch.setattr(httpx, "post", post)
    message = FeishuMessage("incoming", "chat", "问题", "user")
    first = create_agent_reply(settings, sessions, ui.store)(message)
    assert first.run_id == "run-from-backend"
    assert first.user_id == post.call_args.kwargs["json"]["user_id"]
    ui.reply_card.side_effect = RuntimeError("发送失败")
    with pytest.raises(RuntimeError):
        ui.send_answer(first)
    reopened = FeedbackStore(ui.store.path, "app", AGENT_ID)
    second = create_agent_reply(settings, sessions, reopened)(message)
    assert second == first
    post.assert_called_once()
    assert sessions.current("chat").turn_count == 1


def test_worker_sends_answer_card_and_feedback_without_second_llm_call(ui):
    saved = snapshot(ui)
    generate, text_reply = Mock(return_value=saved), Mock()
    handler = FeishuHandler(text_reply, generate, feedback_ui=ui)
    handler._enqueue("incoming", FeishuMessage("incoming", "chat", "问题", "user"))
    handler.start()
    try:
        handler._queue.join()
        assert handler.on_card_action(callback(saved.snapshot_id))["toast"]["type"] == "info"
        handler._queue.join()
        assert ui.store.get_feedback(saved.snapshot_id).rating == "helpful"
    finally:
        handler.stop()
    text_reply.assert_not_called()
    generate.assert_called_once()
