import json
import runpy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from feedback import FeedbackStore


@pytest.fixture
def store(tmp_path):
    return FeedbackStore(tmp_path / "feedback.sqlite3", "app", "dev-issue-agent")


def save_answer(store, **changes):
    values = dict(
        source_message_id="incoming",
        chat_id="chat",
        open_id="user",
        user_id="backend-user",
        thread_id="thread",
        run_id="run",
        question="问题",
        answer="回答",
    )
    values.update(changes)
    return store.save_answer(**values)


def submit(store, snapshot, **changes):
    values = dict(
        snapshot_id=snapshot.snapshot_id,
        open_id="user",
        chat_id="chat",
        card_message_id="card",
        event_id="event",
        rating="helpful",
        comment="建议保留出处",
    )
    values.update(changes)
    return store.submit(**values)


def test_snapshot_is_committed_before_card_and_cannot_be_overwritten(store):
    snapshot = save_answer(store)
    reopened = FeedbackStore(store.path, "app", "dev-issue-agent")
    assert reopened.get_answer(snapshot.snapshot_id) == snapshot
    assert snapshot.card_message_id is None
    assert save_answer(store) == snapshot
    with pytest.raises(ValueError, match="不能覆盖"):
        save_answer(store, answer="另一条回答")
    assert store.get_answer(snapshot.snapshot_id).answer == "回答"


@pytest.mark.parametrize("field", ["open_id", "run_id", "thread_id", "question", "answer"])
def test_snapshot_requires_real_identifiers_and_text(store, field):
    with pytest.raises(ValueError, match="缺少"):
        save_answer(store, **{field: ""})


def test_namespace_isolation_and_source_message_ownership(store):
    snapshot = save_answer(store)
    other = FeedbackStore(store.path, "other-app", "dev-issue-agent")
    with pytest.raises(ValueError, match="不属于"):
        other.get_answer(snapshot.snapshot_id)
    assert save_answer(other).snapshot_id != snapshot.snapshot_id
    with pytest.raises(ValueError, match="不一致"):
        store.find_answer("incoming", "foreign-chat", "user", "问题")


def test_bind_card_validates_chat_and_is_immutable(store):
    snapshot = save_answer(store)
    with pytest.raises(ValueError):
        store.bind_card(snapshot.snapshot_id, "card", "other-chat")
    store.bind_card(snapshot.snapshot_id, "card", "chat")
    store.bind_card(snapshot.snapshot_id, "card", "chat")
    with pytest.raises(ValueError):
        store.bind_card(snapshot.snapshot_id, "other-card", "chat")


def test_feedback_survives_reopen_and_first_submission_wins(store):
    snapshot = save_answer(store)
    store.bind_card(snapshot.snapshot_id, "card", "chat")
    original = submit(store, snapshot)
    assert submit(store, snapshot) == original
    reopened = FeedbackStore(store.path, "app", "dev-issue-agent")
    assert reopened.get_feedback(snapshot.snapshot_id) == original
    assert (
        submit(reopened, snapshot, event_id="another-click", rating="not_helpful", comment="改主意")
        == original
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"open_id": "other-user"},
        {"chat_id": "forwarded-chat"},
        {"card_message_id": "forwarded-card"},
        {"open_id": ""},
        {"event_id": ""},
        {"rating": "invalid"},
        {"comment": "长" * 1001},
    ],
)
def test_invalid_or_foreign_submission_does_not_write(store, changes):
    snapshot = save_answer(store)
    store.bind_card(snapshot.snapshot_id, "card", "chat")
    with pytest.raises(ValueError):
        submit(store, snapshot, **changes)
    assert store.get_feedback(snapshot.snapshot_id) is None


def test_ownership_is_checked_even_after_feedback_exists(store):
    snapshot = save_answer(store)
    store.bind_card(snapshot.snapshot_id, "card", "chat")
    submit(store, snapshot)
    with pytest.raises(ValueError, match="原单聊"):
        submit(store, snapshot, open_id="other-user")


def test_same_event_cannot_rate_different_answers(store):
    first = save_answer(store)
    second = save_answer(store, source_message_id="incoming-2", run_id="run-2")
    store.bind_card(first.snapshot_id, "card", "chat")
    store.bind_card(second.snapshot_id, "card-2", "chat")
    submit(store, first)
    with pytest.raises(ValueError, match="另一条回答"):
        submit(store, second, card_message_id="card-2")
    assert store.get_feedback(second.snapshot_id) is None


def test_concurrent_submissions_keep_one_consistent_record(store):
    snapshot = save_answer(store)
    store.bind_card(snapshot.snapshot_id, "card", "chat")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(submit, store, snapshot, event_id=str(index), rating=rating)
            for index, rating in enumerate(("helpful", "not_helpful"))
        ]
        results = [future.result() for future in futures]
    assert results[0] == results[1] == store.get_feedback(snapshot.snapshot_id)


def test_comment_is_optional_and_trimmed(store):
    snapshot = save_answer(store)
    store.bind_card(snapshot.snapshot_id, "card", "chat")
    assert submit(store, snapshot, comment=" \n ").comment == ""


def test_query_cli_reads_persisted_feedback_and_answer(store, monkeypatch, capsys):
    snapshot = save_answer(store)
    store.bind_card(snapshot.snapshot_id, "card", "chat")
    submit(store, snapshot)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args: None)
    script = Path(__file__).resolve().parents[1] / "scripts/show_feedback.py"
    args = ["show_feedback.py", "--db", str(store.path), "--app-id", "app"]
    monkeypatch.setattr("sys.argv", args)
    runpy.run_path(str(script), run_name="__main__")
    records = json.loads(capsys.readouterr().out)
    assert len(records) == 1 and records[0]["snapshot_id"] == snapshot.snapshot_id
    monkeypatch.setattr("sys.argv", [*args, "--snapshot-id", snapshot.snapshot_id])
    runpy.run_path(str(script), run_name="__main__")
    detail = json.loads(capsys.readouterr().out)
    assert detail["answer"]["answer"] == "回答"
    assert detail["answer"]["run_id"] == "run"
    assert detail["feedback"]["comment"] == "建议保留出处"
