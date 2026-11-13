"""评测 API：启动、轮询、并发限制与逐轮结果回放。"""

import asyncio
import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from core import settings
from service import app, eval_api


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_SECRET", None)
    monkeypatch.setattr(settings, "LANGFUSE_TRACING", False)
    eval_api._JOBS.clear()
    monkeypatch.setattr(eval_api, "_ACTIVE", None)
    yield
    eval_api._JOBS.clear()
    monkeypatch.setattr(eval_api, "_ACTIVE", None)


async def call(method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


def fake_runner(rows: int = 0, summary: dict | None = None, delay: float = 0.0):
    """替身 run_evaluation：按需写 results.jsonl，避免真实调用模型。"""

    async def run(suite, client, output_dir: Path, model=None, timeout=90):
        await asyncio.sleep(delay)
        output_dir.mkdir(parents=True, exist_ok=True)
        with (output_dir / "results.jsonl").open("w", encoding="utf-8") as stream:
            for index in range(rows):
                stream.write(
                    json.dumps(
                        {
                            "case_id": f"SP0{index + 1}",
                            "title": "合成案例",
                            "turn_index": 1,
                            "status": "passed" if index == 0 else "failed",
                            "duration_seconds": 1.5,
                            "run_id": f"run-{index}",
                            "answer": "合成回答",
                            "checks": [{"name": "answer_present", "passed": index == 0}],
                            "attribution": {"stage": "none" if index == 0 else "retrieve"},
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        return summary or {"passed_cases": 1, "stage_failures": {"retrieve": 1}}

    return run


async def wait_for(job_id: str, timeout: float = 3.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        body = (await call("GET", f"/eval/runs/{job_id}")).json()
        if body["status"] in {"succeeded", "failed"}:
            return body
        await asyncio.sleep(0.02)
    raise AssertionError("评测任务没有在预期时间内结束")


@pytest.mark.asyncio
async def test_cases_endpoint_lists_the_fixed_suite():
    body = (await call("GET", "/eval/cases")).json()
    assert len(body["cases"]) == 12
    assert body["cases"][0]["id"] == "SP01"
    assert sum(case["turns"] for case in body["cases"]) == 14


@pytest.mark.asyncio
async def test_start_run_reports_progress_and_results(monkeypatch):
    monkeypatch.setattr(eval_api, "AgentClient", Mock())
    monkeypatch.setattr(eval_api, "run_evaluation", fake_runner(rows=2))
    response = await call("POST", "/eval/runs", json={"case_ids": ["SP01", "SP06"]})
    assert response.status_code == 200
    job = response.json()
    assert job["status"] == "queued"
    assert job["total_cases"] == 2

    body = await wait_for(job["job_id"])
    assert body["status"] == "succeeded"
    assert body["recorded_turns"] == 2
    assert body["passed_cases"] == 1
    assert body["stage_failures"] == {"retrieve": 1}
    assert body["rows"][1]["attribution"]["stage"] == "retrieve"
    assert body["rows"][1]["failed_checks"] == ["answer_present"]
    # 失败行仍保留 run_id，页面据此跳转调用追踪。
    assert body["rows"][1]["run_id"] == "run-1"


@pytest.mark.asyncio
async def test_second_run_is_rejected_while_one_is_active(monkeypatch):
    monkeypatch.setattr(eval_api, "AgentClient", Mock())
    monkeypatch.setattr(eval_api, "run_evaluation", fake_runner(delay=0.2))
    first = await call("POST", "/eval/runs", json={})
    assert first.status_code == 200
    second = await call("POST", "/eval/runs", json={})
    assert second.status_code == 409
    await wait_for(first.json()["job_id"])


@pytest.mark.asyncio
async def test_unknown_case_and_unknown_model_are_rejected(monkeypatch):
    monkeypatch.setattr(eval_api, "AgentClient", Mock())
    monkeypatch.setattr(eval_api, "run_evaluation", fake_runner())
    unknown = await call("POST", "/eval/runs", json={"case_ids": ["SP99"]})
    assert unknown.status_code == 422
    assert "未知案例" in unknown.json()["detail"]
    bad_model = await call("POST", "/eval/runs", json={"model": "not-a-model"})
    assert bad_model.status_code == 422


@pytest.mark.asyncio
async def test_failed_run_records_the_error(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("backend down")

    monkeypatch.setattr(eval_api, "AgentClient", Mock())
    monkeypatch.setattr(eval_api, "run_evaluation", boom)
    job = (await call("POST", "/eval/runs", json={})).json()
    body = await wait_for(job["job_id"])
    assert body["status"] == "failed"
    assert "backend down" in body["error"]


@pytest.mark.asyncio
async def test_missing_job_is_404():
    assert (await call("GET", "/eval/runs/nope")).status_code == 404
