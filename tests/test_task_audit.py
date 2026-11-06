import json
import logging
import runpy
import sqlite3
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import run_feishu
from evaluation_jobs import WorkerLock
from feishu_tasks import DurableTaskWorker, MessageTaskStore
from task_audit.migrations import migrate_task_database

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path):
    return MessageTaskStore(tmp_path / "tasks.sqlite3", json.dumps(["app", "dev-issue-agent"]))


def enqueue(store, key="message"):
    return store.enqueue(key, "FeishuMessage", {"message_id": key, "text": "private-question"}, 32)


def event_types(store, task):
    return [event["event_type"] for event in store.timeline(task.task_id)]


def make_legacy(store):
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TABLE task_events")
        db.execute("DROP TABLE task_schema_migrations")
        db.execute("DROP INDEX task_list_time")


def test_legacy_upgrade_is_repeatable_and_does_not_invent_history(store):
    task = enqueue(store)
    store.update(task.task_id, "ready", reply={"text": "saved-answer"})
    original = store.get(task.task_id)
    make_legacy(store)
    assert migrate_task_database(store.path)
    assert not migrate_task_database(store.path)
    reopened = MessageTaskStore(store.path, store.namespace)
    assert reopened.get(task.task_id) == original
    assert reopened.timeline(task.task_id) == []
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM task_schema_migrations").fetchone()[0] == 1


def test_failed_migration_rolls_back_ddl_and_keeps_data(store):
    task = enqueue(store)
    make_legacy(store)
    with sqlite3.connect(store.path) as db:
        db.execute("CREATE TABLE task_events (unrelated TEXT)")
    with pytest.raises(sqlite3.OperationalError):
        migrate_task_database(store.path)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT task_id FROM feishu_tasks").fetchone()[0] == task.task_id
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='task_schema_migrations'"
        ).fetchone()


def test_missing_or_wrong_database_does_not_get_silently_created(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        migrate_task_database(path)
    assert not path.exists()
    with sqlite3.connect(path):
        pass
    with pytest.raises(ValueError, match="缺少"):
        migrate_task_database(path)


def test_duplicate_events_and_context_are_idempotent(store):
    task = enqueue(store)
    for _ in range(3):
        enqueue(store)
        store.record_invocation("message", "thread", "user")
        store.record_agent_result("message", "run")
    assert event_types(store, task) == ["task_created", "agent_requested", "agent_returned"]
    assert store.get(task.task_id).context["run_id"] == "run"
    with pytest.raises(ValueError):
        store.record_invocation("message", "different-thread", "user")
    with pytest.raises(ValueError):
        store.record_agent_result("message", "different-run")


def test_audit_failure_rolls_back_business_state_and_emits_no_success(store, caplog):
    task = enqueue(store)
    store.update(task.task_id, "ready", reply={"text": "answer"})
    with sqlite3.connect(store.path) as db:
        db.execute("""CREATE TRIGGER block_audit BEFORE INSERT ON task_events
            WHEN NEW.event_type='task_succeeded' BEGIN SELECT RAISE(ABORT,'test failure'); END""")
    caplog.set_level(logging.INFO, logger="task_audit.events")
    with pytest.raises(sqlite3.IntegrityError):
        store.update(task.task_id, "succeeded")
    assert store.get(task.task_id).state == "ready"
    assert "task_succeeded" not in event_types(store, task)
    assert "task_succeeded" not in caplog.text


def test_events_and_logs_omit_body_and_error_text(store, caplog):
    caplog.set_level(logging.INFO, logger="task_audit.events")
    task = enqueue(store)
    store.update(task.task_id, "needs_review", error="private-provider-error")
    assert "unknown_execution" in caplog.text
    assert "private-question" not in json.dumps(store.timeline(task.task_id))
    assert "private-provider-error" not in caplog.text
    with pytest.raises(ValueError, match="正文"):
        store.record_event(task.task_id, "invalid", details={"prompt": "private-prompt"})


def test_namespace_isolation_and_timeline_pagination(store):
    task = enqueue(store)
    store.update(task.task_id, "invoking")
    store.update(task.task_id, "ready", reply={"text": "answer"})
    first = store.timeline(task.task_id, limit=2)
    second = store.timeline(task.task_id, after_event_id=first[-1]["event_id"], limit=2)
    assert len(first) == 2 and len(second) == 1
    other = MessageTaskStore(store.path, "other")
    with pytest.raises(ValueError):
        other.timeline(task.task_id)
    assert other.recent() == []


def test_query_time_zone_boundaries_and_filters(store):
    task = enqueue(store)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE feishu_tasks SET created_at=?", ("2026-11-06T02:00:00+00:00",))
    assert (
        store.recent(since="2026-11-06T10:00:00", until="2026-11-06T10:01:00")[0].task_id
        == task.task_id
    )
    assert store.recent(until="2026-11-06T10:00:00") == []
    assert store.recent(message_id="message", state="queued", kind="FeishuMessage")
    assert store.recent(message_id="different") == []
    with pytest.raises(ValueError):
        store.recent(since="2026-11-07", until="2026-11-06")


def test_recovery_is_audited_idempotent_and_does_not_reset_later_success(store, caplog):
    task = enqueue(store)
    store.update(task.task_id, "failed", reply={"text": "answer"}, error="send failed")
    caplog.set_level(logging.INFO, logger="task_audit.events")
    kwargs = dict(actor="operator", reason="private operator note", operation_id="op-1")
    assert store.recover(task.task_id, "retry-send", **kwargs).state == "ready"
    store.update(task.task_id, "succeeded")
    assert store.recover(task.task_id, "retry-send", **kwargs).state == "succeeded"
    events = [e for e in store.timeline(task.task_id) if e["event_type"] == "manual_recovery"]
    assert len(events) == 1
    assert events[0]["reason"] == kwargs["reason"]
    assert events[0]["actor"] == "operator"
    assert "private operator note" not in caplog.text
    with pytest.raises(ValueError):
        store.recover(task.task_id, "retry-send", **{**kwargs, "reason": "different"})


def test_recovery_requires_stopped_worker_and_correct_state(store):
    task = enqueue(store)
    kwargs = dict(actor="operator", reason="checked", operation_id="op")
    with pytest.raises(ValueError):
        store.recover(task.task_id, "retry-send", **kwargs)
    store.update(task.task_id, "needs_review", error="unknown")
    with pytest.raises(ValueError):
        store.recover(task.task_id, "retry-send", **kwargs)
    with pytest.raises(ValueError):
        store.recover(task.task_id, "abandon", **kwargs)
    lock = WorkerLock(store.path.with_suffix(".worker.lock"))
    lock.acquire()
    try:
        with pytest.raises(RuntimeError):
            store.recover(task.task_id, "abandon", backend_checked=True, **kwargs)
    finally:
        lock.release()
    assert (
        store.recover(task.task_id, "abandon", backend_checked=True, **kwargs).state == "abandoned"
    )


def test_old_notification_versions_do_not_duplicate_or_override_delivery_events(store):
    task = enqueue(store)
    store.ensure_progress(task.task_id)
    store.ensure_progress(task.task_id)
    store.bind_progress_card(task.task_id, "card", "")
    store.bind_progress_card(task.task_id, "card", "")
    old = store.get_progress(task.task_id).version
    store.set_progress_target(task.task_id, "processing")
    store.progress_failed(task.task_id, old, "private-error", 0)
    store.progress_delivered(task.task_id, old)
    store.progress_delivered(task.task_id, old)
    assert event_types(store, task).count("notification_registered") == 1
    assert event_types(store, task).count("card_bound") == 1
    assert "delivery_failed" not in event_types(store, task)
    delivered = store.timeline(task.task_id)[-1]
    assert delivered["details"]["target_state"] == "superseded"
    assert (
        store.get_progress(task.task_id).delivered_version
        < store.get_progress(task.task_id).version
    )


def test_worker_restart_records_recovery_before_dispatch(store):
    task = enqueue(store)
    stream = Mock()
    stream.cleanup.return_value = []
    completed = Event()

    def finish(task):
        store.update(task.task_id, "succeeded")
        completed.set()

    dispatch = Mock(side_effect=finish)
    worker = DurableTaskWorker(store, stream, dispatch)
    worker.start()
    try:
        assert completed.wait(5)
    finally:
        worker.stop()
    assert "recovery_scheduled" in event_types(store, task)
    assert event_types(store, task).index("recovery_scheduled") < event_types(store, task).index(
        "task_succeeded"
    )


def test_cli_latest_timeline_preview_and_reason_validation(store, monkeypatch, capsys):
    task = enqueue(store)
    store.enqueue("menu", "FeishuAction", {}, 32)
    monkeypatch.setattr(
        run_feishu,
        "FeishuSettings",
        lambda: SimpleNamespace(FEISHU_TASK_DB_PATH=store.path, FEISHU_APP_ID="app"),
    )
    cli = runpy.run_path(str(ROOT / "scripts/show_message_tasks.py"))["main"]
    cli(["--latest", "--timeline"])
    output = capsys.readouterr().out
    result = json.loads(output)[0]
    assert result["task_id"] == task.task_id
    assert result["events"][0]["event_type"] == "task_created"
    assert "private-question" not in output
    cli(["--message-id", "message", "--preview", "--format", "table"])
    assert "private-question" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli(["--task-id", task.task_id, "--action", "abandon"])
    assert store.get(task.task_id).state == "queued"
    capsys.readouterr()
    store.update(task.task_id, "failed", reply={"text": "saved answer"})
    command = [
        "--task-id",
        task.task_id,
        "--action",
        "retry-send",
        "--reason",
        "checked",
        "--actor",
        "tester",
        "--operation-id",
        "cli-op",
        "--timeline",
    ]
    cli(command)
    audit = json.loads(capsys.readouterr().out)[0]
    assert audit["operation_id"] == "cli-op"
    assert audit["events"][-1]["actor"] == "tester"
    cli(command)
    assert len(json.loads(capsys.readouterr().out)[0]["events"]) == len(audit["events"])


def test_migration_cli_backup_contains_original_and_repeat_is_noop(
    store, tmp_path, capsys, monkeypatch
):
    task = enqueue(store)
    make_legacy(store)
    cli = runpy.run_path(str(ROOT / "scripts/migrate_task_db.py"))["main"]
    # The CLI normally loads .env; this test must not leak real settings to SDK subprocess tests.
    monkeypatch.setitem(cli.__globals__, "load_dotenv", lambda *args: None)
    cli(["--db", str(store.path), "--backup-dir", str(tmp_path / "backups")])
    output = json.loads(capsys.readouterr().out)
    assert output["applied"]
    with sqlite3.connect(output["backup"]) as backup:
        assert backup.execute("SELECT task_id FROM feishu_tasks").fetchone()[0] == task.task_id
        assert not backup.execute("SELECT 1 FROM sqlite_master WHERE name='task_events'").fetchone()
    cli(["--db", str(store.path)])
    assert not json.loads(capsys.readouterr().out)["applied"]


def test_failed_recovery_audit_rolls_back_notification_reset(store):
    task = enqueue(store)
    store.ensure_progress(task.task_id)
    store.set_progress_target(task.task_id, "answered", "snapshot")
    version = store.get_progress(task.task_id).version
    store.progress_failed(task.task_id, version, "ConnectionError", 0)
    store.update(task.task_id, "failed", reply={"snapshot_id": "snapshot"})
    original = store.get_progress(task.task_id)
    with sqlite3.connect(store.path) as db:
        db.execute("""CREATE TRIGGER block_recovery BEFORE INSERT ON task_events
            WHEN NEW.event_type='manual_recovery' BEGIN SELECT RAISE(ABORT,'test'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        store.recover(
            task.task_id, "retry-send", actor="operator", reason="checked", operation_id="op"
        )
    assert store.get(task.task_id).state == "failed"
    assert store.get_progress(task.task_id) == original
