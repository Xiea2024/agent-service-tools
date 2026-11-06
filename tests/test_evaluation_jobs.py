import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import AsyncMock, Mock

import pytest

from evaluation import Case, Suite, Turn
from evaluation_jobs import (
    EvaluationJobStore,
    EvaluationWorker,
    JobBusyError,
    WorkerLock,
    evaluate_job,
)


@pytest.fixture
def suite():
    return Suite(
        id="fixed",
        version="1",
        description="合成案例",
        cases=[
            Case(
                id="one",
                title="问题",
                category="test",
                turns=[Turn(message="你好", rubric=["人工检查"])],
            )
        ],
    )


@pytest.fixture
def store(tmp_path):
    return EvaluationJobStore(tmp_path / "jobs.sqlite3", "app", "dev-issue-agent")


def result():
    return {
        "status": "completed",
        "passed_cases": 0,
        "total_cases": 1,
        "turn_counts": {"passed": 0, "failed": 1, "error": 0},
        "duration_seconds": 1.2,
    }


def test_duplicate_events_and_busy_clicks_do_not_start_another_job(store, suite, tmp_path):
    first, created = store.create("event", "user", suite, tmp_path / "runs")
    assert created
    assert store.create("event", "user", suite, tmp_path / "runs") == (first, False)
    assert store.create("event-2", "user", suite, tmp_path / "runs") == (first, False)
    with pytest.raises(JobBusyError):
        store.create("event-3", "other-user", suite, tmp_path / "runs")
    with pytest.raises(ValueError):
        store.get(first.job_id, "other-user")
    assert store.latest("other-user") is None
    store.update(first.job_id, status="failed")
    assert store.create("event", "user", suite, tmp_path / "runs")[0].job_id == first.job_id
    second, created = store.create("event-4", "user", suite, tmp_path / "runs")
    assert created and second.job_id != first.job_id
    assert store.latest("user").job_id == second.job_id


def test_concurrent_creation_has_one_active_task(store, suite, tmp_path):
    def create(index):
        try:
            return store.create(str(index), str(index), suite, tmp_path / "runs")[1]
        except JobBusyError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(executor.map(create, [1, 2])) == 1


@pytest.mark.parametrize("state", ["queued", "running", "publishing"])
def test_restart_marks_active_jobs_interrupted_without_reexecution(store, suite, tmp_path, state):
    job, _ = store.create("event", "user", suite, tmp_path / "runs")
    store.update(job.job_id, status=state, document_id="partialdoc")
    reopened = EvaluationJobStore(store.path, "app", "dev-issue-agent")
    reopened.recover()
    recovered = reopened.get(job.job_id)
    assert recovered.status == "interrupted" and recovered.error_stage == "restart"
    assert recovered.output_dir == job.output_dir and recovered.document_id == "partialdoc"
    assert reopened.claim() is None
    assert reopened.pending_notifications()[0].job_id == job.job_id


def test_second_worker_does_not_recover_live_process_jobs(store, suite, tmp_path):
    lock = WorkerLock(store.path.with_suffix(".worker.lock"))
    lock.acquire()
    try:
        job, _ = store.create("event", "user", suite, tmp_path / "runs")
        worker = EvaluationWorker(store, Mock(), Mock(), Mock())
        with pytest.raises(RuntimeError, match="已有进程"):
            worker.start()
        assert store.get(job.job_id).status == "queued"
    finally:
        lock.release()


@pytest.mark.parametrize(
    "failure,status",
    [(None, "succeeded"), ("evaluation", "failed"), ("publication", "publish_failed")],
)
def test_worker_records_each_failure_phase_and_keeps_local_report(
    store, suite, tmp_path, failure, status
):
    job, _ = store.create("event", "user", suite, tmp_path / "runs")

    def evaluate(job, stop):
        if failure == "evaluation":
            raise RuntimeError("后端不可用")
        Path(job.output_dir).mkdir(parents=True)
        (Path(job.output_dir) / "report.md").write_text("# 本地报告", encoding="utf-8")
        return result()

    def publish(job, stop):
        assert job.status == "publishing" and job.summary["passed_cases"] == 0
        store.update(job.job_id, document_id="doc123")
        if failure == "publication":
            raise RuntimeError("阅读授权失败")
        return "https://tenant.feishu.cn/docx/doc123"

    notify = Mock()
    worker = EvaluationWorker(store, evaluate, publish, notify)
    worker.execute(store.claim())
    saved = store.get(job.job_id)
    assert saved.status == status
    assert saved.notified_at and not saved.notification_error
    notify.assert_called_once()
    if failure != "evaluation":
        assert (Path(job.output_dir) / "report.md").is_file()
        assert saved.document_id == "doc123" and saved.summary == result()
    if failure:
        assert saved.report_url is None
        assert saved.error_stage == ("evaluation" if failure == "evaluation" else "publication")
    else:
        assert saved.report_url.endswith("/docx/doc123")


def test_failed_notification_is_persistent_and_retried_after_restart(store, suite, tmp_path):
    job, _ = store.create("event", "user", suite, tmp_path / "runs")
    job = store.update(
        job.job_id, status="succeeded", report_url="https://tenant.feishu.cn/docx/doc123"
    )
    broken = EvaluationWorker(store, Mock(), Mock(), Mock(side_effect=RuntimeError("发送失败")))
    broken.notify_saved(job)
    assert store.get(job.job_id).status == "succeeded"
    assert "发送失败" in store.get(job.job_id).notification_error
    sent = Event()
    good = EvaluationWorker(store, Mock(), Mock(), lambda _: sent.set())
    good.start()
    try:
        assert sent.wait(2)
    finally:
        good.stop()
    saved = store.get(job.job_id)
    assert saved.notified_at and saved.notification_error is None


def test_stopping_worker_interrupts_work_and_retains_notification_for_restart(
    store, suite, tmp_path
):
    entered = Event()

    def evaluate(job, stop):
        entered.set()
        stop.wait(2)
        raise asyncio.CancelledError

    notify = Mock()
    worker = EvaluationWorker(store, evaluate, Mock(), notify)
    worker.start()
    job, _ = store.create("event", "user", suite, tmp_path / "runs")
    worker.wake.set()
    assert entered.wait(2)
    worker.stop()
    assert store.get(job.job_id).status == "interrupted"
    notify.assert_not_called()


@pytest.mark.asyncio
async def test_background_adapter_reuses_existing_runner_and_snapshot(
    store, suite, tmp_path, monkeypatch
):
    job, _ = store.create("event", "user", suite, tmp_path / "runs")
    client = Mock()
    client.info.models = ["fake"]
    factory = Mock(return_value=client)
    runner = AsyncMock(return_value=result())
    monkeypatch.setattr("evaluation_jobs.AgentClient", factory)
    monkeypatch.setattr("evaluation_jobs.run_evaluation", runner)
    response = await evaluate_job(job, Event(), "http://backend", "test-secret", "fake", 75)
    assert response == result()
    assert client.auth_secret == "test-secret" and client.timeout == 75
    client.retrieve_info.assert_called_once()
    assert runner.await_args.args[0] == suite
    assert runner.await_args.args[2] == Path(job.output_dir)
    assert runner.await_args.args[3:] == ("fake", 75)


@pytest.mark.asyncio
async def test_background_adapter_cancels_runner_on_shutdown(store, suite, tmp_path, monkeypatch):
    job, _ = store.create("event", "user", suite, tmp_path / "runs")
    client = Mock()
    monkeypatch.setattr("evaluation_jobs.AgentClient", Mock(return_value=client))

    async def long_run(*args):
        await asyncio.sleep(10)

    monkeypatch.setattr("evaluation_jobs.run_evaluation", long_run)
    stop = Event()
    stop.set()
    with pytest.raises(asyncio.CancelledError):
        await evaluate_job(job, stop, "http://backend", None)


def test_job_namespace_isolation(store, suite, tmp_path):
    job, _ = store.create("event", "user", suite, tmp_path / "runs")
    other = EvaluationJobStore(store.path, "other-app", "dev-issue-agent")
    with pytest.raises(ValueError):
        other.get(job.job_id)
    assert other.latest("user") is None
