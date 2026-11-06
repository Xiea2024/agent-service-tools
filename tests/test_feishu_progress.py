import sqlite3
from dataclasses import asdict
from threading import Event
from unittest.mock import Mock

import pytest

from feedback import FeedbackStore
from feishu_feedback import FeedbackAction, FeishuFeedbackUI
from feishu_progress import FeishuProgressUI
from feishu_tasks import DurableTaskWorker, MessageTaskStore
from run_feishu import AGENT_ID, FeishuHandler, FeishuMessage


@pytest.fixture
def setup(tmp_path):
    store = MessageTaskStore(tmp_path / "tasks.sqlite3", "app")
    feedback = FeedbackStore(tmp_path / "feedback.sqlite3", "app", AGENT_ID)
    cards, updates = [], []

    def create(message_id, card):
        cards.append((message_id, card))
        return f"card:{message_id}", "chat"

    ui = FeishuFeedbackUI(
        feedback, Mock(side_effect=create), Mock(side_effect=lambda *a: updates.append(a)), Mock()
    )

    def generate(item):
        return feedback.save_answer(
            source_message_id=item.message_id,
            chat_id=item.chat_id,
            open_id=item.open_id,
            user_id="user",
            thread_id="thread",
            run_id=f"run:{item.message_id}",
            question=item.text,
            answer="saved answer",
        )

    handler = FeishuHandler(Mock(), Mock(side_effect=generate), feedback_ui=ui)
    stream = Mock()
    stream.cleanup.return_value = []
    stream.take.side_effect = lambda task_id: task_id
    progress = FeishuProgressUI(store, ui, retry_delay=0)
    worker = DurableTaskWorker(
        store, stream, handler._handle_durable, retry_delay=0.01, on_saved=progress.schedule
    )
    handler.tasks, handler.progress = worker, progress
    return handler, worker, progress, ui, cards, updates


def enqueue(setup, message_id="message"):
    handler, worker, *_ = setup
    handler._enqueue(message_id, FeishuMessage(message_id, "chat", "question", "owner"))
    return worker.store.recent()[0]


def test_second_message_receipt_is_independent_of_blocked_model(setup):
    handler, worker, progress, ui, cards, _ = setup
    entered, release, received_second = Event(), Event(), Event()
    original = handler._generate_reply.side_effect

    def blocked(item):
        entered.set()
        assert release.wait(5)
        return original(item)

    def create(message_id, card):
        cards.append((message_id, card))
        if message_id == "second":
            received_second.set()
        return f"card:{message_id}", "chat"

    handler._generate_reply.side_effect = blocked
    ui.reply_card.side_effect = create
    handler.start()
    try:
        enqueue(setup)
        assert entered.wait(2)
        enqueue(setup, "second")
        assert received_second.wait(2)
        assert not release.is_set()
        assert handler._generate_reply.call_count == 1
        second = next(card for message_id, card in cards if message_id == "second")
        assert second["header"]["title"]["content"] == "已收到"
        assert all(element["tag"] != "form" for element in second["body"]["elements"])
    finally:
        release.set()
        handler.stop()


def test_receipt_then_answer_updates_same_card_and_binds_before_buttons(setup):
    handler, worker, progress, ui, cards, updates = setup
    task = enqueue(setup)
    progress.step()
    worker.step()
    assert worker.store.get(task.task_id).state == "ready"
    assert worker.store.get(task.task_id).attempts == 0

    def patch(card_id, card):
        snapshot_id = card["body"]["elements"][-1]["elements"][-1]["behaviors"][0]["value"][
            "snapshot_id"
        ]
        snapshot = ui.store.get_answer(snapshot_id)
        assert snapshot.answer == "saved answer" and snapshot.card_message_id == card_id
        updates.append((card_id, card))

    ui.update_card.side_effect = patch
    progress.step()
    worker.step()
    assert len(cards) == len(updates) == 1
    assert updates[0][0] == "card:message"
    assert worker.store.get(task.task_id).state == "succeeded"
    handler._generate_reply.assert_called_once()


def test_duplicate_and_stale_processing_do_not_replace_final_or_feedback(setup):
    handler, worker, progress, ui, cards, updates = setup
    task = enqueue(setup)
    enqueue(setup)
    progress.step()
    worker.step()
    progress.step()
    worker.step()
    snapshot_id = worker.store.get(task.task_id).reply["snapshot_id"]
    ui.handle(FeedbackAction("event", "owner", "chat", "card:message", snapshot_id, "helpful", ""))
    before = len(updates)
    worker.store.set_progress_target(task.task_id, "queued")
    worker.store.set_progress_target(task.task_id, "processing")
    progress.step()
    enqueue(setup)
    progress.step()
    assert len(cards) == 1 and len(updates) == before
    assert worker.store.get_progress(task.task_id).desired_state == "answered"
    assert "反馈已保存" in str(updates[-1][1])
    handler._generate_reply.assert_called_once()


def test_notification_failure_retries_and_manual_resume_never_reinvokes(setup):
    handler, worker, progress, ui, cards, updates = setup
    task = enqueue(setup)
    progress.step()
    worker.step()
    ui.update_card.side_effect = ConnectionError("offline")
    for _ in range(4):
        progress.step()
        worker.step()
    result = worker.store.get(task.task_id)
    notice = worker.store.get_progress(task.task_id)
    assert result.state == "failed" and result.reply["snapshot_id"]
    assert notice.attempts == 3 and notice.error == "ConnectionError"
    handler._generate_reply.assert_called_once()
    assert len(cards) == 1
    worker.store.update(task.task_id, "ready", reset_attempts=True)
    ui.update_card.side_effect = lambda *args: updates.append(args)
    worker.step()
    progress.step()
    worker.step()
    assert worker.store.get(task.task_id).state == "succeeded"
    handler._generate_reply.assert_called_once()


def test_restart_resumes_bound_but_unpublished_answer_on_original_card(setup):
    handler, worker, progress, ui, cards, updates = setup
    task = enqueue(setup)
    progress.step()
    worker.step()
    ui.update_card.side_effect = ConnectionError()
    progress.step()
    snapshot_id = worker.store.get(task.task_id).reply["snapshot_id"]
    assert ui.store.get_answer(snapshot_id).card_message_id == "card:message"
    reopened = MessageTaskStore(worker.store.path, "app")
    restarted = FeishuProgressUI(reopened, ui, retry_delay=0)
    handler.progress = restarted
    ui.update_card.side_effect = lambda *args: updates.append(args)
    restarted.step()
    worker.step()
    assert reopened.get(task.task_id).state == "succeeded"
    assert len(cards) == 1 and updates[-1][0] == "card:message"
    handler._generate_reply.assert_called_once()


def test_retry_after_feedback_during_ambiguous_patch_preserves_feedback(setup):
    handler, worker, progress, ui, cards, updates = setup
    task = enqueue(setup)
    progress.step()
    worker.step()
    ui.update_card.side_effect = TimeoutError()
    progress.step()
    snapshot_id = worker.store.get(task.task_id).reply["snapshot_id"]
    ui.store.submit(
        snapshot_id=snapshot_id,
        open_id="owner",
        chat_id="chat",
        card_message_id="card:message",
        event_id="feedback",
        rating="not_helpful",
    )
    ui.update_card.side_effect = lambda *args: updates.append(args)
    progress.step()
    worker.step()
    assert "反馈已保存：没帮助" in str(updates[-1][1])
    assert all(e["tag"] != "form" for e in updates[-1][1]["body"]["elements"])
    handler._generate_reply.assert_called_once()


def test_unknown_invocation_updates_existing_card_and_never_retries_model(setup):
    handler, worker, progress, ui, cards, updates = setup
    task = enqueue(setup)
    progress.step()
    worker.store.update(task.task_id, "invoking")
    progress.step()
    worker.step()
    progress.step()
    assert worker.store.get(task.task_id).state == "needs_review"
    assert updates[-1][0] == "card:message"
    assert updates[-1][1]["header"]["title"]["content"] == "处理结果待确认"
    assert len(cards) == 1
    handler._generate_reply.assert_not_called()
    handler._send_reply.assert_not_called()


def test_connection_failure_updates_card_without_locking_chat(setup):
    import httpx

    handler, worker, progress, ui, cards, updates = setup
    original = handler._generate_reply.side_effect
    handler._generate_reply.side_effect = httpx.ConnectError("refused")
    first = enqueue(setup)
    progress.step()
    worker.step()
    progress.step()
    assert worker.store.get(first.task_id).state == "failed"
    assert updates[-1][1]["header"]["title"]["content"] == "问答服务暂不可用"
    assert "本条未执行" in str(updates[-1][1])
    handler._generate_reply.side_effect = original
    second = enqueue(setup, "second")
    worker.step()
    progress.step()
    worker.step()
    assert worker.store.get(second.task_id).state == "succeeded"


def test_blocked_message_does_not_become_another_uncertain_invocation(setup):
    handler, worker, progress, ui, cards, updates = setup
    first = enqueue(setup)
    worker.store.update(first.task_id, "needs_review", error="unknown backend result")
    second = enqueue(setup, "second")
    worker.step()
    progress.step()
    assert worker.store.get(second.task_id).state == "blocked"
    assert updates[-1][1]["header"]["title"]["content"] == "本条尚未处理"
    worker.store.update(first.task_id, "abandoned")
    third = enqueue(setup, "third")
    worker.step()
    progress.step()
    worker.step()
    assert worker.store.get(third.task_id).state == "succeeded"
    assert handler._generate_reply.call_count == 1


def test_old_database_gets_notification_table_without_rewriting_tasks(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    original = MessageTaskStore(path, "app")
    task = original.enqueue(
        "incoming", "FeishuMessage", asdict(FeishuMessage("incoming", "chat", "q", "owner")), 32
    )
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE feishu_task_progress")
    reopened = MessageTaskStore(path, "app")
    assert reopened.get(task.task_id) == task
    reopened.ensure_progress(task.task_id)
    assert reopened.get_progress(task.task_id).desired_state == "queued"
    other = MessageTaskStore(path, "another-app")
    assert other.get_progress(task.task_id) is None


def test_restart_adopts_legacy_final_card_instead_of_sending_another(setup):
    handler, worker, progress, ui, cards, updates = setup
    task = enqueue(setup)
    snapshot = handler._generate_reply(FeishuMessage("message", "chat", "question", "owner"))
    ui.store.bind_card(snapshot.snapshot_id, "legacy-card", "chat")
    worker.store.update(task.task_id, "invoking")
    progress.step()
    assert not cards and not updates
    worker.step()
    progress.step()
    worker.step()
    assert not cards and updates[-1][0] == "legacy-card"
    assert worker.store.get(task.task_id).state == "succeeded"


def test_pending_receipt_survives_redis_publish_failure(setup):
    handler, worker, progress, ui, cards, updates = setup
    worker.stream.publish.side_effect = ConnectionError()
    with pytest.raises(RuntimeError, match="任务已保存"):
        enqueue(setup)
    progress.step()
    assert cards[0][1]["header"]["title"]["content"] == "已收到"
    assert len(worker.store.active()) == 1
    handler._generate_reply.assert_not_called()


def test_new_target_is_not_acknowledged_by_older_inflight_notice(setup):
    handler, worker, progress, ui, cards, updates = setup
    task = enqueue(setup)
    original = ui.reply_card.side_effect

    def create_then_finish(message_id, card):
        result = original(message_id, card)
        worker.step()
        return result

    ui.reply_card.side_effect = create_then_finish
    progress.step()
    record = worker.store.get_progress(task.task_id)
    assert record.delivered_version < record.version
    progress.step()
    worker.step()
    assert worker.store.get(task.task_id).state == "succeeded"
    assert updates[-1][1]["header"]["title"]["content"] == "SupportPilot 回答"


def test_process_crash_during_final_patch_recovers_same_card_with_real_redis(tmp_path):
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path
    from uuid import uuid4

    from feishu_tasks import RedisTaskStream

    url = os.getenv("SUPPORTPILOT_TEST_REDIS_URL")
    if not url:
        pytest.skip("需要真实 Redis 才能验证通知进程强杀恢复")
    namespace = f"progress-crash-{uuid4()}"
    stream = RedisTaskStream.connect(url, namespace, 60)
    script = r"""
import sys
from pathlib import Path
from threading import Event
from unittest.mock import Mock
sys.path.insert(0, 'src')
from feedback import FeedbackStore
from feishu_feedback import FeishuFeedbackUI
from feishu_progress import FeishuProgressUI
from feishu_tasks import DurableTaskWorker, MessageTaskStore, RedisTaskStream
from run_feishu import AGENT_ID, FeishuHandler, FeishuMessage
root, url, namespace = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
store = MessageTaskStore(root / 'tasks.sqlite3', 'app')
feedback = FeedbackStore(root / 'feedback.sqlite3', 'app', AGENT_ID)
def generate(item):
    return feedback.save_answer(source_message_id='message', chat_id='chat', open_id='owner',
        user_id='user', thread_id='thread', run_id='run', question='question', answer='answer')
def patch(message_id, card):
    if card['body']['elements'][-1]['tag'] == 'form':
        (root / 'entered').write_text('bound_before_patch')
        Event().wait(120)
ui = FeishuFeedbackUI(feedback, Mock(return_value=('original-card', 'chat')), patch, Mock())
handler = FeishuHandler(Mock(), generate, feedback_ui=ui)
progress = FeishuProgressUI(store, ui)
stream = RedisTaskStream.connect(url, namespace, 60)
worker = DurableTaskWorker(store, stream, handler._handle_durable, retry_delay=0.01,
    on_saved=progress.schedule)
handler.tasks, handler.progress = worker, progress
handler.start()
handler._enqueue('message', FeishuMessage('message', 'chat', 'question', 'owner'))
Event().wait(120)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), url, namespace],
        cwd=Path(__file__).parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 15
        while not (tmp_path / "entered").exists() and process.poll() is None:
            if time.monotonic() > deadline:
                pytest.fail("未进入答案卡片更新中断位置")
            Event().wait(0.02)
        if process.poll() is not None:
            pytest.fail(process.communicate()[1].decode(errors="replace"))
        process.kill()
        process.communicate(timeout=5)
        store = MessageTaskStore(tmp_path / "tasks.sqlite3", "app")
        feedback = FeedbackStore(tmp_path / "feedback.sqlite3", "app", AGENT_ID)
        task = store.recent()[0]
        record = store.get_progress(task.task_id)
        assert record.card_message_id == "original-card"
        assert record.delivered_version < record.version
        ui = FeishuFeedbackUI(feedback, Mock(), Mock(), Mock())
        handler = FeishuHandler(Mock(), Mock(), feedback_ui=ui)
        progress = FeishuProgressUI(store, ui)
        worker = DurableTaskWorker(store, stream, handler._handle_durable)
        handler.tasks, handler.progress = worker, progress
        worker.lock.acquire()
        try:
            progress.step()
            worker.step()
        finally:
            worker.lock.release()
        assert store.get(task.task_id).state == "succeeded"
        ui.reply_card.assert_not_called()
        assert ui.update_card.call_args.args[0] == "original-card"
        handler._generate_reply.assert_not_called()
        assert stream.client.xlen(stream.stream) == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
        keys = list(stream.client.scan_iter(match=f"{stream.stream}*"))
        if keys:
            stream.client.delete(*keys)
        stream.close()
