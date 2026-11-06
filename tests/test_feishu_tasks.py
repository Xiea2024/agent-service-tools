import os
from threading import Event
from unittest.mock import Mock
from uuid import uuid4

import pytest

from feedback import FeedbackStore
from feishu_feedback import FeishuFeedbackUI
from feishu_tasks import DurableTaskWorker, MessageTaskStore, RedisTaskStream
from run_feishu import AGENT_ID, FeishuHandler, FeishuMessage


class LocalStream:
    def __init__(self):
        self.entries = {}
        self.available = True

    def initialize(self):
        if not self.available:
            raise ConnectionError("offline")

    def publish(self, task_id):
        self.initialize()
        self.entries.setdefault(task_id, task_id)
        return task_id

    def take(self, task_id):
        return self.publish(task_id)

    def finish(self, entry_id):
        self.initialize()
        self.entries.pop(entry_id, None)

    def cleanup(self):
        return [(key, {"task_id": key}) for key in self.entries]

    def close(self):
        pass


@pytest.fixture
def setup(tmp_path):
    store = MessageTaskStore(tmp_path / "tasks.sqlite3", "app")
    feedback = FeedbackStore(tmp_path / "feedback.sqlite3", "app", AGENT_ID)
    ui = FeishuFeedbackUI(feedback, Mock(return_value=("card", "chat")), Mock(), Mock())
    generate, send = Mock(return_value="answer"), Mock()
    handler = FeishuHandler(send, generate, feedback_ui=ui)
    stream = LocalStream()
    worker = DurableTaskWorker(store, stream, handler._handle_durable, retry_delay=0.01)
    handler.tasks = worker
    return handler, worker, ui, generate, send


def enqueue(setup, message_id="message", chat_id="chat"):
    handler, worker, *_ = setup
    item = FeishuMessage(message_id, chat_id, "question", "owner")
    handler._enqueue(message_id, item)
    return worker.store.recent()[0]


def save_answer(ui, message_id="message"):
    return ui.store.save_answer(
        source_message_id=message_id,
        chat_id="chat",
        open_id="owner",
        user_id="user",
        thread_id="thread",
        run_id="run",
        question="question",
        answer="answer",
    )


def test_duplicate_is_persistent_and_payload_mismatch_rejected(setup):
    handler, worker, _, generate, send = setup
    task = enqueue(setup)
    enqueue(setup)
    worker.step()
    handler._enqueue("message", FeishuMessage("message", "chat", "question", "owner"))
    reopened = MessageTaskStore(worker.store.path, "app")
    assert len(reopened.recent()) == 1
    assert reopened.get(task.task_id).state == "succeeded"
    generate.assert_called_once()
    events = [e["event_type"] for e in reopened.timeline(task.task_id)]
    assert (
        events.count("task_created")
        == events.count("agent_started")
        == events.count("task_succeeded")
        == 1
    )
    send.assert_called_once_with("message", "answer")
    with pytest.raises(ValueError, match="不一致"):
        worker.enqueue("message", "FeishuMessage", {"text": "changed"})


def test_restart_before_agent_executes_once(setup):
    handler, old, _, generate, _ = setup
    task = enqueue(setup)
    restarted = DurableTaskWorker(
        MessageTaskStore(old.store.path, "app"), old.stream, handler._handle_durable
    )
    handler.tasks = restarted
    restarted.step()
    assert restarted.store.get(task.task_id).state == "succeeded"
    generate.assert_called_once()
    assert restarted.store.timeline(task.task_id)[0]["event_type"] == "task_created"
    assert restarted.store.timeline(task.task_id)[-1]["event_type"] == "task_succeeded"


def test_restart_after_snapshot_reuses_answer_without_model(setup):
    _, worker, ui, generate, _ = setup
    task = enqueue(setup)
    worker.store.update(task.task_id, "invoking")
    answer = save_answer(ui)
    worker.step()
    generate.assert_not_called()
    assert ui.store.get_answer(answer.snapshot_id).card_message_id == "card"
    assert worker.store.get(task.task_id).state == "succeeded"
    assert "answer_reused" in [e["event_type"] for e in worker.store.timeline(task.task_id)]


def test_snapshot_send_failure_then_success_does_not_reinvoke(setup):
    _, worker, ui, generate, _ = setup
    task = enqueue(setup)
    generate.side_effect = lambda _: save_answer(ui)
    ui.reply_card.side_effect = [ConnectionError("send failed"), ("card", "chat")]
    worker.step()
    assert worker.store.get(task.task_id).state == "ready"
    worker.step()
    assert worker.store.get(task.task_id).state == "succeeded"
    generate.assert_called_once()
    assert ui.reply_card.call_count == 2


def test_exception_after_snapshot_commit_recovers_without_reinvoking(setup):
    _, worker, ui, generate, _ = setup
    task = enqueue(setup)

    def generate_then_fail(_):
        save_answer(ui)
        raise RuntimeError("session counter failed after answer commit")

    generate.side_effect = generate_then_fail
    worker.step()
    assert worker.store.get(task.task_id).state == "succeeded"
    generate.assert_called_once()
    ui.reply_card.assert_called_once()


def test_already_sent_snapshot_avoids_resend_after_crash(setup):
    _, worker, ui, generate, _ = setup
    task = enqueue(setup)
    answer = save_answer(ui)
    ui.store.bind_card(answer.snapshot_id, "old-card", "chat")
    worker.store.update(task.task_id, "invoking")
    worker.step()
    generate.assert_not_called()
    ui.reply_card.assert_not_called()
    assert worker.store.get(task.task_id).state == "succeeded"


def test_uncertain_invocation_does_not_retry_or_run_later_same_chat(setup):
    _, worker, _, generate, send = setup
    first = enqueue(setup)
    worker.store.update(first.task_id, "invoking")
    second = enqueue(setup, "second")
    other = enqueue(setup, "other", "other-chat")
    for _ in range(3):
        worker.step()
    assert worker.store.get(first.task_id).state == "needs_review"
    assert worker.store.get(second.task_id).state == "blocked"
    assert worker.store.get(other.task_id).state == "succeeded"
    assert generate.call_count == 1
    assert send.call_count == 3


def test_agent_timeout_is_recorded_and_never_retried(setup):
    _, worker, _, generate, _ = setup
    task = enqueue(setup)
    generate.side_effect = TimeoutError()
    worker.step()
    worker.step()
    assert worker.store.get(task.task_id).state == "needs_review"
    generate.assert_called_once()
    assert worker.store.timeline(task.task_id)[-1]["event_type"] == "needs_review"


def test_audited_retry_send_preserves_answer_and_never_reinvokes_model(setup):
    _, worker, _, generate, send = setup
    task = enqueue(setup)
    send.side_effect = [ConnectionError(), ConnectionError(), ConnectionError(), None]
    for _ in range(3):
        worker.step()
    assert worker.store.get(task.task_id).state == "failed"
    worker.store.recover(
        task.task_id,
        "retry-send",
        actor="operator",
        reason="消息网络已恢复",
        operation_id="retry-1",
    )
    worker.step()
    assert worker.store.get(task.task_id).state == "succeeded"
    generate.assert_called_once()
    assert send.call_count == 4
    kinds = [e["event_type"] for e in worker.store.timeline(task.task_id)]
    assert kinds.count("retry_scheduled") == 2
    assert kinds.count("retry_exhausted") == kinds.count("manual_recovery") == 1
    assert kinds[-1] == "task_succeeded"


def test_unavailable_backend_does_not_poison_later_messages(setup, monkeypatch):
    import httpx

    from run_feishu import FeishuSettings, create_agent_reply

    handler, worker, ui, _, _ = setup
    settings = FeishuSettings(_env_file=None, FEISHU_APP_ID="app", FEISHU_APP_SECRET="fake")
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs["json"])
        request = httpx.Request("POST", url)
        if len(calls) == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(
            200, request=request, json={"type": "ai", "content": "recovered", "run_id": "run"}
        )

    monkeypatch.setattr(httpx, "post", post)
    handler._generate_reply = create_agent_reply(settings, feedback=ui.store)
    first = enqueue(setup)
    worker.step()
    assert worker.store.get(first.task_id).state == "failed"
    assert "ConnectError" in worker.store.get(first.task_id).error
    second = enqueue(setup, "second")
    worker.step()
    assert worker.store.get(second.task_id).state == "succeeded"
    assert len(calls) == 2


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("connect", True),
        ("connect_timeout", True),
        ("pool_timeout", True),
        ("read_timeout", False),
        ("write_timeout", False),
        ("protocol", False),
        (401, True),
        (403, True),
        (404, True),
        (405, True),
        (422, True),
        (500, False),
        (503, False),
    ],
)
def test_request_failure_classification_uses_original_exception(failure, expected):
    import httpx

    from client import AgentClientError
    from run_feishu import request_not_started

    request = httpx.Request("POST", "http://localhost/agent/invoke")
    if isinstance(failure, int):
        response = httpx.Response(failure, request=request)
        error = httpx.HTTPStatusError("status", request=request, response=response)
    else:
        kind = {
            "connect": httpx.ConnectError,
            "connect_timeout": httpx.ConnectTimeout,
            "pool_timeout": httpx.PoolTimeout,
            "read_timeout": httpx.ReadTimeout,
            "write_timeout": httpx.WriteTimeout,
            "protocol": httpx.RemoteProtocolError,
        }[failure]
        error = kind("simulated", request=request)
    try:
        try:
            raise error
        except httpx.HTTPError:
            raise AgentClientError("wrapped")
    except AgentClientError as exc:
        assert (request_not_started(exc) is not None) is expected


def test_redis_failure_preserves_sql_outbox(setup):
    handler, worker, _, generate, _ = setup
    worker.stream.available = False
    with pytest.raises(RuntimeError, match="任务已保存"):
        enqueue(setup)
    assert len(worker.store.active()) == 1
    worker.stream.available = True
    worker.step()
    generate.assert_called_once()


def test_ack_failure_does_not_repeat_business_execution(setup):
    _, worker, _, generate, _ = setup
    task = enqueue(setup)
    original = worker.stream.finish
    worker.stream.finish = Mock(side_effect=ConnectionError())
    with pytest.raises(ConnectionError):
        worker.step()
    assert worker.store.get(task.task_id).state == "succeeded"
    worker.stream.finish = original
    worker.step()
    generate.assert_called_once()
    assert not worker.stream.entries


def test_permanent_send_failure_has_bounded_attempts(setup):
    _, worker, _, generate, send = setup
    task = enqueue(setup)
    send.side_effect = ConnectionError()
    for _ in range(5):
        worker.step()
    result = worker.store.get(task.task_id)
    assert result.state == "failed" and result.attempts == 3
    assert result.reply == {"text": "answer"}
    generate.assert_called_once()
    assert send.call_count == 3


def test_capacity_and_namespace_isolation(setup):
    _, worker, *_ = setup
    for index in range(32):
        enqueue(setup, str(index))
    with pytest.raises(RuntimeError, match="队列已满"):
        enqueue(setup, "overflow")
    other = MessageTaskStore(worker.store.path, "other-app")
    assert other.recent() == []
    with pytest.raises(ValueError):
        other.get(worker.store.recent()[0].task_id)


def test_second_worker_cannot_start_against_same_store(setup):
    _, worker, *_ = setup
    worker.start()
    other = DurableTaskWorker(worker.store, LocalStream(), Mock())
    try:
        with pytest.raises(RuntimeError):
            other.start()
    finally:
        worker.stop()


def test_recovery_preserves_question_and_session_action_order(setup):
    from run_feishu import FeishuAction

    handler, worker, _, generate, send = setup
    handler._session_ui = Mock()
    order = []
    generate.side_effect = lambda item: order.append(item.message_id) or "answer"
    handler._session_ui.handle.side_effect = lambda action: order.append(action.action)
    enqueue(setup, "first")
    handler._enqueue("action:new", FeishuAction("new", "owner", "session_new"))
    enqueue(setup, "second")
    for _ in range(3):
        worker.step()
    assert order == ["first", "session_new", "second"]


@pytest.fixture
def real_stream():
    url = os.getenv("SUPPORTPILOT_TEST_REDIS_URL")
    if not url:
        pytest.skip("设置 SUPPORTPILOT_TEST_REDIS_URL 可运行真实 Redis 集成测试")
    stream = RedisTaskStream.connect(url, f"test-{uuid4()}", ttl=60)
    stream.initialize()
    try:
        yield stream
    finally:
        keys = list(stream.client.scan_iter(match=f"{stream.stream}*"))
        if keys:
            stream.client.delete(*keys)
        stream.close()


def test_real_stream_atomic_dedup_across_clients_and_pending_recovery(real_stream, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=8) as executor:
        ids = list(executor.map(real_stream.publish, ["one"] * 20))
    assert len(set(ids)) == 1
    assert real_stream.client.xlen(real_stream.stream) == 1
    assert real_stream.take("one") == ids[0]
    assert real_stream.client.xpending(real_stream.stream, real_stream.group)["pending"] == 1
    assert real_stream.take("one") == ids[0]
    assert 0 < real_stream.client.ttl(f"{real_stream.stream}:dedup:one") <= 60
    real_stream.finish(ids[0])
    assert real_stream.client.xpending(real_stream.stream, real_stream.group)["pending"] == 0
    assert real_stream.client.xlen(real_stream.stream) == 0


def test_real_stream_worker_recovery_after_redis_entry_loss(real_stream, tmp_path):
    store = MessageTaskStore(tmp_path / "tasks.sqlite3", "app")
    order = []
    worker = DurableTaskWorker(store, real_stream, lambda task: order.append(task.event_key))
    first = worker.enqueue("first", "action", {})
    worker.enqueue("second", "action", {})
    old_id = real_stream.publish(first.task_id)
    real_stream.client.xdel(real_stream.stream, old_id)
    worker.step()
    worker.step()
    assert order == ["first", "second"]
    assert real_stream.client.xlen(real_stream.stream) == 0


def test_real_handler_stream_worker_calls_existing_bridge(real_stream, tmp_path):
    import httpx

    from feishu_sessions import FeishuSessionStore
    from run_feishu import FeishuSettings, create_agent_reply

    feedback = FeedbackStore(tmp_path / "feedback.sqlite3", "app", AGENT_ID)
    sessions = FeishuSessionStore(tmp_path / "sessions.sqlite3", "app", AGENT_ID)
    settings = FeishuSettings(_env_file=None, FEISHU_APP_ID="app", FEISHU_APP_SECRET="fake")
    requests = []
    finished = Event()
    ui = FeishuFeedbackUI(feedback, Mock(return_value=("card", "chat")), Mock(), Mock())
    handler = FeishuHandler(
        Mock(), create_agent_reply(settings, sessions, feedback), feedback_ui=ui
    )
    store = MessageTaskStore(tmp_path / "tasks.sqlite3", "app")
    worker = DurableTaskWorker(store, real_stream, handler._handle_durable, retry_delay=0.01)
    handler.tasks = worker

    def post(url, **kwargs):
        requests.append(kwargs["json"])
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"type": "ai", "content": "answer", "run_id": "run"},
        )

    original = httpx.post
    httpx.post = post
    worker.start()
    try:
        item = FeishuMessage("message", "chat", "question", "owner")
        handler._enqueue("message", item)
        handler._enqueue("message", item)
        for _ in range(100):
            if store.recent() and store.recent()[0].state == "succeeded":
                break
            finished.wait(0.02)
        assert store.recent()[0].state == "succeeded"
        assert len(requests) == 1
        assert requests[0]["thread_id"] == sessions.current("chat", "owner").thread_id
        assert ui.store.find_answer("message", "chat", "owner", "question").run_id == "run"
    finally:
        worker.stop()
        httpx.post = original


@pytest.mark.parametrize("stage", ["queued", "invoking", "snapshot"])
def test_real_stream_recovers_after_process_is_killed(real_stream, tmp_path, stage):
    import subprocess
    import sys
    import time
    from pathlib import Path

    script = r"""
import sys
from pathlib import Path
from threading import Event
from unittest.mock import Mock
sys.path.insert(0, 'src')
from feedback import FeedbackStore
from feishu_feedback import FeishuFeedbackUI
from feishu_tasks import DurableTaskWorker, MessageTaskStore, RedisTaskStream
from run_feishu import AGENT_ID, FeishuHandler, FeishuMessage
root, stage, url, stream_name = Path(sys.argv[1]), *sys.argv[2:]
store = MessageTaskStore(root / 'tasks.sqlite3', 'app')
feedback = FeedbackStore(root / 'feedback.sqlite3', 'app', AGENT_ID)
stream = RedisTaskStream.connect(url, 'temporary', 60)
stream.stream = stream_name
def block():
    (root / 'entered').write_text('ready')
    Event().wait(120)
def generate(message):
    if stage == 'invoking':
        block()
    return feedback.save_answer(source_message_id='message', chat_id='chat', open_id='owner',
        user_id='user', thread_id='thread', run_id='run', question='question', answer='answer')
def send(*args):
    block()
ui = FeishuFeedbackUI(feedback, send, Mock(), Mock())
handler = FeishuHandler(Mock(), generate, feedback_ui=ui)
worker = DurableTaskWorker(store, stream, handler._handle_durable)
handler.tasks = worker
handler._enqueue('message', FeishuMessage('message', 'chat', 'question', 'owner'))
if stage == 'queued':
    block()
worker.start()
Event().wait(120)
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path),
            stage,
            os.environ["SUPPORTPILOT_TEST_REDIS_URL"],
            real_stream.stream,
        ],
        cwd=Path(__file__).parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 15
        while not (tmp_path / "entered").exists() and process.poll() is None:
            if time.monotonic() > deadline:
                pytest.fail("子进程未进入预期中断位置")
            Event().wait(0.02)
        if process.poll() is not None:
            _, stderr = process.communicate()
            pytest.fail(stderr.decode(errors="replace"))
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
    store = MessageTaskStore(tmp_path / "tasks.sqlite3", "app")
    feedback = FeedbackStore(tmp_path / "feedback.sqlite3", "app", AGENT_ID)
    ui = FeishuFeedbackUI(feedback, Mock(return_value=("card", "chat")), Mock(), Mock())
    generate = Mock(return_value="answer")
    handler = FeishuHandler(Mock(), generate, feedback_ui=ui)
    worker = DurableTaskWorker(store, real_stream, handler._handle_durable)
    handler.tasks = worker
    worker.lock.acquire()
    try:
        worker.step()
    finally:
        worker.lock.release()
    result = store.recent()[0]
    if stage == "invoking":
        assert result.state == "needs_review"
        generate.assert_not_called()
    elif stage == "snapshot":
        assert result.state == "succeeded"
        generate.assert_not_called()
        ui.reply_card.assert_called_once()
    else:
        assert result.state == "succeeded"
        generate.assert_called_once()
