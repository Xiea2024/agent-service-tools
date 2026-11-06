import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.request import urlopen

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry, generate_latest

from feishu_tasks import MessageTaskStore
from observability.http import install_metrics
from observability.metrics import CALLS, operation
from observability.tasks import STATES, TaskCollector


def export(worker):
    registry = CollectorRegistry()
    registry.register(TaskCollector(worker))
    return generate_latest(registry).decode()


def worker(store):
    return SimpleNamespace(store=store, thread=Mock(is_alive=lambda: True), last_poll_timestamp=123)


def test_durable_counts_are_scoped_deduplicated_and_content_free(tmp_path):
    store = MessageTaskStore(tmp_path / "tasks.sqlite3", "app")
    task = store.enqueue("private-event", "FeishuMessage", {"text": "private-question"}, 32)
    store.enqueue("private-event", "FeishuMessage", {"text": "private-question"}, 32)
    store.update(task.task_id, "needs_review", error="secret-token")
    other = MessageTaskStore(store.path, "other")
    other.enqueue("other-event", "FeishuMessage", {}, 32)
    # Menu/feedback tasks must not inflate question success rates.
    menu = store.enqueue("menu", "FeishuAction", {}, 32)
    store.update(menu.task_id, "succeeded")
    first = export(worker(store))
    reopened = export(worker(MessageTaskStore(store.path, "app")))
    assert first == reopened
    assert 'supportpilot_tasks{state="needs_review"} 1.0' in first
    assert 'supportpilot_tasks{state="queued"} 0.0' in first
    assert 'supportpilot_tasks{state="succeeded"} 0.0' in first
    for value in ("secret-token", "private-question", "private-event", task.task_id):
        assert value not in first


def test_latency_window_and_active_age(tmp_path):
    store = MessageTaskStore(tmp_path / "tasks.sqlite3", "app")
    successful = store.enqueue("success", "FeishuMessage", {}, 32)
    store.update(successful.task_id, "succeeded")
    store.enqueue("pending", "FeishuMessage", {}, 32)
    old = store.enqueue("old", "FeishuMessage", {}, 32)
    store.update(old.task_id, "failed")
    now = datetime.now(UTC)
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE feishu_tasks SET created_at=?", ((now - timedelta(seconds=30)).isoformat(),)
        )
        db.execute(
            "UPDATE feishu_tasks SET updated_at=? WHERE task_id=?",
            ((now - timedelta(hours=2)).isoformat(), old.task_id),
        )
    output = export(worker(store))
    assert 'supportpilot_tasks_last_hour{state="succeeded"} 1.0' in output
    assert 'supportpilot_tasks_last_hour{state="failed"} 0.0' in output
    assert "supportpilot_task_average_duration_last_hour_seconds " in output
    age = next(
        line
        for line in output.splitlines()
        if line.startswith("supportpilot_oldest_active_task_age_seconds ")
    )
    assert 29 <= float(age.split()[-1]) < 40


def test_missing_database_is_unavailable_and_never_created(tmp_path):
    path = tmp_path / "missing.sqlite3"
    output = export(worker(SimpleNamespace(path=path, namespace="app")))
    assert "supportpilot_task_store_up 0.0" in output
    assert not path.exists()
    assert "supportpilot_tasks{" not in output


def test_exporter_http_lifecycle_and_restart(tmp_path, monkeypatch):
    import observability.tasks as tasks

    store = MessageTaskStore(tmp_path / "tasks.sqlite3", "app")
    original = tasks.start_http_server
    servers = []

    def capture(*args, **kwargs):
        server, thread = original(*args, **kwargs)
        servers.append(server)
        return server, thread

    monkeypatch.setattr(tasks, "start_http_server", capture)
    for _ in range(2):
        stop = tasks.start_task_metrics(worker(store), "127.0.0.1", 0)
        try:
            with urlopen(
                f"http://127.0.0.1:{servers[-1].server_port}/metrics", timeout=5
            ) as response:
                text = response.read().decode()
            assert "supportpilot_task_store_up 1.0" in text
            assert "supportpilot_worker_alive 1.0" in text
        finally:
            stop()


def test_no_completed_tasks_does_not_invent_latency(tmp_path):
    store = MessageTaskStore(tmp_path / "tasks.sqlite3", "app")
    assert "\nsupportpilot_task_average_duration_last_hour_seconds " not in export(worker(store))


def test_failure_reason_labels_are_bounded(tmp_path):
    store = MessageTaskStore(tmp_path / "tasks.sqlite3", "app")
    rejected = store.enqueue("reject", "FeishuMessage", {}, 32)
    store.update(rejected.task_id, "failed", error="请求未发送或被拒绝：ConnectError")
    delivery = store.enqueue("send", "FeishuMessage", {}, 32)
    store.update(delivery.task_id, "failed", reply={"text": "private-answer"})
    output = export(worker(store))
    assert 'supportpilot_task_failures{reason="backend_unavailable_or_rejected"} 1.0' in output
    assert 'supportpilot_task_failures{reason="delivery"} 1.0' in output
    assert "private-answer" not in output
    assert "ConnectError" not in output


@pytest.mark.asyncio
async def test_handled_retrieval_error_records_failed_operation(monkeypatch):
    import document_retrieval

    errors = CALLS.labels("retrieval", "error")
    success = CALLS.labels("retrieval", "success")
    before_error, before_success = errors._value.get(), success._value.get()
    monkeypatch.setattr(
        document_retrieval,
        "search_documents",
        Mock(side_effect=document_retrieval.RetrievalError("qdrant_unavailable", "合成失败")),
    )
    result = await document_retrieval.search_feishu_documents.ainvoke({"query": "合成问题"})
    assert result["status"] == "error"
    assert errors._value.get() == before_error + 1
    assert success._value.get() == before_success


def test_operation_exception_and_handled_error_are_not_success():
    samples = CALLS.labels("test-model", "timeout")
    before = samples._value.get()
    with pytest.raises(TimeoutError), operation("test-model"):
        raise TimeoutError("private-error")
    assert samples._value.get() == before + 1
    errors = CALLS.labels("test-retrieval", "error")
    before = errors._value.get()
    with operation("test-retrieval") as result:
        result["outcome"] = "error"
    assert errors._value.get() == before + 1


def test_http_routes_are_bounded_and_metrics_are_scrapable():
    app = FastAPI()
    install_metrics(app)

    @app.get("/items/{item_id}")
    def item(item_id: str):
        return {"ok": True}

    @app.get("/broken")
    def broken():
        raise RuntimeError("private-exception")

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/items/private-id").status_code == 200
        assert client.get("/random-private-url").status_code == 404
        assert client.get("/broken").status_code == 500
        response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert 'route="/items/{item_id}"' in response.text
    assert 'route="unmatched",status="404"' in response.text
    assert 'route="/broken",status="500"' in response.text
    for value in ("private-id", "random-private-url", "private-exception"):
        assert value not in response.text


def test_dashboard_and_alerts_reference_provisioned_datasource():
    root = Path(__file__).resolve().parents[2] / "monitoring/grafana"
    dashboard = json.loads((root / "dashboards/supportpilot.json").read_text(encoding="utf-8"))
    alerts = json.loads((root / "provisioning/alerting/rules.json").read_text(encoding="utf-8"))
    assert dashboard["uid"] == "supportpilot-overview"
    assert len({p["id"] for p in dashboard["panels"]}) == len(dashboard["panels"])
    for panel in dashboard["panels"]:
        assert panel["datasource"]["uid"] == "supportpilot-prometheus"
        assert "task_id" not in panel["targets"][0]["expr"]
    for rule in alerts["groups"][0]["rules"]:
        assert rule["data"][0]["datasourceUid"] == "supportpilot-prometheus"
    assert len(STATES) == len(set(STATES))
