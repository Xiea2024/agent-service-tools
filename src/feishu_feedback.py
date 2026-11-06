"""Connect Feishu answer cards and authenticated callback context to the feedback store."""

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from typing import Any

from feedback import MAX_COMMENT_LENGTH, AnswerSnapshot, FeedbackStore, Rating
from feishu_cards import answer_card, notice_card

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeedbackAction:
    event_id: str
    open_id: str
    chat_id: str
    card_message_id: str
    snapshot_id: str
    rating: Rating
    comment: str


def parse_feedback_action(data: dict[str, Any]) -> FeedbackAction:
    event, header = data.get("event") or {}, data.get("header") or {}
    if not isinstance(event, dict) or not isinstance(header, dict):
        raise ValueError("反馈事件格式不正确")
    action = event.get("action") or {}
    context, operator = event.get("context") or {}, event.get("operator") or {}
    if any(not isinstance(item, dict) for item in (action, context, operator)):
        raise ValueError("反馈事件格式不正确")
    value = action.get("value") or {}
    form = action.get("form_value") or {}
    if not isinstance(value, dict) or not isinstance(form, dict):
        raise ValueError("反馈数据格式不正确")

    def required_text(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("请从原单聊的回答卡片提交反馈")
        return value

    fields = (
        required_text(header.get("event_id")),
        required_text(operator.get("open_id")),
        required_text(context.get("open_chat_id")),
        required_text(context.get("open_message_id")),
        required_text(value.get("snapshot_id")),
    )
    rating, comment = value.get("rating"), form.get("comment", "")
    if comment is None:
        comment = ""
    if rating not in ("helpful", "not_helpful") or value.get("action") != "feedback_submit":
        raise ValueError("请选择有帮助或没帮助")
    if not isinstance(comment, str) or len(comment) > MAX_COMMENT_LENGTH:
        raise ValueError("意见最多 1000 字")
    return FeedbackAction(*fields, rating, comment)


class FeishuFeedbackUI:
    def __init__(
        self,
        store: FeedbackStore,
        reply_card: Callable[[str, dict[str, Any]], tuple[str, str]],
        update_card: Callable[[str, dict[str, Any]], None],
        send_card: Callable[[str, str, dict[str, Any], str], tuple[str, str]],
    ):
        self.store, self.reply_card = store, reply_card
        self.update_card, self.send_card = update_card, send_card
        self.card_lock = RLock()

    def send_answer(self, snapshot: AnswerSnapshot) -> None:
        current = self.store.get_answer(snapshot.snapshot_id)
        if current.card_message_id:
            return
        message_id, chat_id = self.reply_card(current.source_message_id, answer_card(current))
        self.store.bind_card(current.snapshot_id, message_id, chat_id)
        logger.info("回答卡片已发送：snapshot_id=%s message_id=%s", current.snapshot_id, message_id)

    def complete_answer(self, snapshot: AnswerSnapshot, message_id: str, chat_id: str) -> None:
        with self.card_lock:
            self.store.bind_card(snapshot.snapshot_id, message_id, chat_id)
            current = self.store.get_answer(snapshot.snapshot_id)
            record = self.store.get_feedback(snapshot.snapshot_id)
            self.update_card(message_id, answer_card(current, record))
        logger.info(
            "回答卡片已更新：snapshot_id=%s message_id=%s", snapshot.snapshot_id, message_id
        )

    def handle(self, action: FeedbackAction) -> None:
        try:
            record = self.store.submit(
                snapshot_id=action.snapshot_id,
                open_id=action.open_id,
                chat_id=action.chat_id,
                card_message_id=action.card_message_id,
                event_id=action.event_id,
                rating=action.rating,
                comment=action.comment,
            )
        except (ValueError, sqlite3.Error):
            logger.exception("反馈保存失败")
            self.send_card(
                "open_id",
                action.open_id,
                notice_card("反馈未保存", "无法保存反馈。请在你与机器人的原单聊回答卡片上重试。"),
                f"feedback-error:{action.event_id}",
            )
            return
        logger.info("回答反馈已保存：snapshot_id=%s rating=%s", record.snapshot_id, record.rating)
        try:
            with self.card_lock:
                snapshot = self.store.get_answer(action.snapshot_id)
                self.update_card(action.card_message_id, answer_card(snapshot, record))
        except Exception:
            logger.exception("反馈已保存，但卡片更新失败")
            self.send_card(
                "open_id",
                action.open_id,
                notice_card("反馈已保存", "感谢评价。卡片暂时未能更新，你的反馈已经保存。"),
                f"feedback-saved:{action.event_id}",
            )
