"""评测 API：从网页启动一次固定案例评测，并回放逐轮结果与阶段归因。

设计约束，都与项目的单进程单 Worker 部署假设一致：

- **同时只允许一个评测任务**。评测会打满后端预算，并行跑两个只会让两边的
  耗时和超时互相污染；第二个请求直接返回 409。
- **前端用轮询而不是 SSE**。评测本身每轮几十秒，进度粒度是“第几轮”，
  轮询一秒一次足够；少一套流式协议就少一类断线问题。
- **不接受客户端传路径**。案例集固定为仓库内的一份文件，可用
  ``EVAL_CASES_PATH`` 覆盖；避免用请求参数读到任意文件。
- **不复制评测逻辑**。任务直接调用 ``evaluation.run_evaluation``，与 CLI 和
  飞书后台共用同一条调用路径、同一份 JSONL 格式。

任务状态只保存在进程内存里：服务重启后历史任务丢失，但结果目录仍在
``evals/supportpilot/runs/`` 下，不会丢数据。
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from client import AgentClient
from core import settings
from evaluation import load_suite, run_evaluation, timestamp

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = ROOT / "evals/supportpilot/cases.json"
RUNS_DIR = ROOT / "evals/supportpilot/runs"
# 只回传页面需要的字段；messages 体积大且页面不用，留在 results.jsonl 里。
ROW_FIELDS = ("case_id", "title", "turn_index", "status", "duration_seconds", "run_id", "answer")

router = APIRouter(prefix="/eval", tags=["eval"])


class EvalRunInput(BaseModel):
    """页面提交的启动参数；不放路径、不放超时以外的执行细节。"""

    case_ids: list[str] = Field(default_factory=list, max_length=50)
    model: str | None = None
    timeout: float = Field(default=90, gt=0, le=300)


@dataclass
class EvalJob:
    """一次进程内评测任务的可见状态。"""

    job_id: str
    output_dir: Path
    total_cases: int
    total_turns: int
    status: str = "queued"
    requested_model: str | None = None
    started_at: str = field(default_factory=timestamp)
    finished_at: str | None = None
    error: str | None = None
    summary: dict[str, Any] | None = None

    def to_dict(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "run_dir": str(self.output_dir.relative_to(ROOT)) if self.output_dir else None,
            "total_cases": self.total_cases,
            "total_turns": self.total_turns,
            "recorded_turns": len(rows),
            "passed_cases": (self.summary or {}).get("passed_cases"),
            "stage_failures": (self.summary or {}).get("stage_failures"),
            "requested_model": self.requested_model,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "rows": rows,
        }


_JOBS: dict[str, EvalJob] = {}
_ACTIVE: str | None = None


def cases_path() -> Path:
    override = os.getenv("EVAL_CASES_PATH")
    return Path(override).resolve() if override else DEFAULT_CASES


def service_url() -> str:
    """评测通过 HTTP 调用自己所在的这个服务，与 CLI 走同一条链路。"""
    return os.getenv("AGENT_URL") or f"http://127.0.0.1:{settings.PORT}"


def _rows(output_dir: Path) -> list[dict[str, Any]]:
    """边跑边读 results.jsonl；最后一行可能只写了一半，解析失败就跳过。"""
    path = output_dir / "results.jsonl"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        row = {key: record.get(key) for key in ROW_FIELDS}
        row["attribution"] = record.get("attribution")
        row["failed_checks"] = [
            check["name"] for check in record.get("checks") or [] if not check.get("passed")
        ]
        rows.append(row)
    return rows


def _job_or_404(job_id: str) -> EvalJob:
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="评测任务不存在或已随服务重启丢失")
    return job


async def _execute(job: EvalJob, suite: Any, model: str | None, timeout: float) -> None:
    global _ACTIVE
    job.status = "running"
    try:
        # AgentClient 的构造函数是同步的（会请求 /info），放到线程里避免阻塞事件循环。
        client = await asyncio.to_thread(
            AgentClient,
            service_url(),
            "dev-issue-agent",
            min(timeout, 5),
        )
        client.timeout = timeout
        job.summary = await run_evaluation(suite, client, job.output_dir, model, timeout)
        job.status = "succeeded"
    except Exception as exc:  # 任务失败要如实记录，页面据此提示
        job.status = "failed"
        job.error = f"{type(exc).__name__}: {exc}"
    finally:
        job.finished_at = timestamp()
        if _ACTIVE == job.job_id:
            _ACTIVE = None


@router.get("/cases")
async def eval_cases() -> dict[str, Any]:
    """列出当前案例集，供页面选择要跑的子集。"""
    suite = load_suite(cases_path())
    return {
        "suite_id": suite.id,
        "version": suite.version,
        "cases": [
            {
                "id": case.id,
                "title": case.title,
                "category": case.category,
                "turns": len(case.turns),
            }
            for case in suite.cases
        ],
    }


@router.get("/runs")
async def eval_runs() -> dict[str, Any]:
    """最近的任务，最新在前；用于页面刷新后恢复上一次结果。"""
    jobs = sorted(_JOBS.values(), key=lambda job: job.started_at, reverse=True)[:10]
    return {"jobs": [job.to_dict(_rows(job.output_dir)) for job in jobs]}


@router.get("/runs/{job_id}")
async def eval_run(job_id: str) -> dict[str, Any]:
    """单个任务的进度与逐轮结果；前端按固定间隔轮询这个接口。"""
    job = _job_or_404(job_id)
    return job.to_dict(_rows(job.output_dir))


@router.post("/runs")
async def start_eval(payload: EvalRunInput) -> dict[str, Any]:
    """启动一次评测；同一时间只允许一个任务。"""
    global _ACTIVE
    if _ACTIVE is not None:
        raise HTTPException(status_code=409, detail="已有评测任务在执行，请等待它结束")
    try:
        suite = load_suite(cases_path(), payload.case_ids or None)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if payload.model and payload.model not in {str(model) for model in settings.AVAILABLE_MODELS}:
        raise HTTPException(status_code=422, detail="服务未提供该模型")

    output_dir = RUNS_DIR / f"{datetime.fromisoformat(timestamp()):%Y%m%d-%H%M%S}-{uuid4().hex[:8]}"
    job = EvalJob(
        job_id=uuid4().hex,
        output_dir=output_dir,
        total_cases=len(suite.cases),
        total_turns=sum(len(case.turns) for case in suite.cases),
        requested_model=payload.model,
    )
    _JOBS[job.job_id] = job
    _ACTIVE = job.job_id
    # 不等待：启动请求必须立刻返回 job_id，否则页面要等整轮评测才刷新。
    asyncio.create_task(_execute(job, suite, payload.model, payload.timeout))
    return job.to_dict([])
