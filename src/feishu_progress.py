"""Publish persisted reply stages on a notification thread independent of model execution."""

import logging
import time
from threading import Event, Thread

from feedback import AnswerSnapshot
from feishu_cards import progress_card
from feishu_feedback import FeishuFeedbackUI
from feishu_tasks import DeliveryPending, MessageTask, MessageTaskStore

logger = logging.getLogger(__name__)


class FeishuProgressUI:
    def __init__(self, store: MessageTaskStore, feedback: FeishuFeedbackUI, retry_delay=2):
        self.store, self.feedback, self.retry_delay = store, feedback, retry_delay
        self.stop_event, self.wake = Event(), Event()
        self.thread = Thread(target=self._run, name="feishu-notifications", daemon=True)

    def schedule(self, task: MessageTask):
        if task.kind == "FeishuMessage":
            self.store.ensure_progress(task.task_id)
            self.wake.set()

    def start(self):
        for task in self.store.active():
            self.schedule(task)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake.set()
        if self.thread.is_alive():
            self.thread.join()

    def answer(self, task_id: str, snapshot: AnswerSnapshot):
        self.store.ensure_progress(task_id)
        self.store.set_progress_target(task_id, "answered", snapshot.snapshot_id)
        self.wake.set()
        progress = self.store.get_progress(task_id)
        if progress is None:
            raise ValueError("回答没有对应的通知记录")
        if progress.delivered_version >= progress.version:
            return
        if progress.attempts >= 3:
            self.store.update(task_id, "failed", error="回答卡片发布失败已达重试上限；答案已保存")
            return
        raise DeliveryPending

    def _snapshot(self, task: MessageTask) -> AnswerSnapshot | None:
        return self.feedback.store.find_answer(
            task.payload["message_id"],
            task.chat_id,
            task.payload["open_id"],
            task.payload["text"],
        )

    def publish(self, task: MessageTask):
        progress = self.store.get_progress(task.task_id)
        if progress is None:
            return
        snapshot = self._snapshot(task)
        if snapshot and snapshot.card_message_id:
            self.store.bind_progress_card(task.task_id, snapshot.card_message_id, snapshot.chat_id)
            # An existing final card must not be replaced by a recovered processing notice.
            if task.state not in {"ready", "succeeded", "failed"}:
                return
        if snapshot and task.state in {"ready", "succeeded", "failed"}:
            self.store.set_progress_target(task.task_id, "answered", snapshot.snapshot_id)
        elif task.state == "invoking":
            self.store.set_progress_target(task.task_id, "processing")
        elif task.state in {"needs_review", "abandoned"}:
            self.store.set_progress_target(task.task_id, "needs_review")
        elif task.state == "blocked":
            self.store.set_progress_target(task.task_id, "blocked")
        elif task.state == "failed":
            self.store.set_progress_target(task.task_id, "failed")
        progress = self.store.get_progress(task.task_id)
        if progress is None or progress.delivered_version >= progress.version:
            return
        if progress.attempts >= 3 or progress.retry_at > time.time():
            return
        try:
            self.store.progress_attempt(task.task_id, progress.version)
            card_id = progress.card_message_id
            created = card_id is None
            if card_id is None:
                card_id, chat_id = self.feedback.reply_card(
                    task.payload["message_id"], progress_card("queued")
                )
                self.store.bind_progress_card(task.task_id, card_id, chat_id)
            if progress.desired_state == "answered":
                if snapshot is None or snapshot.snapshot_id != progress.snapshot_id:
                    raise ValueError("通知与回答快照不一致")
                self.feedback.complete_answer(snapshot, card_id, task.chat_id)
            elif not created or progress.desired_state != "queued":
                self.feedback.update_card(
                    card_id,
                    progress_card(
                        progress.desired_state,
                        request_rejected=(task.error or "").startswith("请求未发送或被拒绝"),
                    ),
                )
            self.store.progress_delivered(task.task_id, progress.version)
            logger.info("回复状态已展示：task_id=%s state=%s", task.task_id, progress.desired_state)
        except Exception as exc:
            self.store.progress_failed(
                task.task_id, progress.version, type(exc).__name__, self.retry_delay
            )
            logger.exception(
                "回复卡片通知失败：task_id=%s state=%s", task.task_id, progress.desired_state
            )

    def step(self):
        for task in self.store.progress_tasks():
            if self.stop_event.is_set():
                return
            try:
                self.publish(task)
            except Exception:
                logger.exception("读取回复状态失败：task_id=%s", task.task_id)

    def _run(self):
        while not self.stop_event.is_set():
            self.wake.clear()
            try:
                self.step()
            except Exception:
                logger.exception("回复通知线程暂不可用，已保存状态保留")
            self.wake.wait(0.2)
