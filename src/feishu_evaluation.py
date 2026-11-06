"""Feishu menu controls and completion cards for durable evaluation jobs."""

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evaluation import load_suite
from evaluation_jobs import EvaluationJob, EvaluationJobStore, EvaluationWorker, JobBusyError
from feishu_cards import notice_card

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvaluationAction:
    event_id: str
    open_id: str
    action: str
    job_id: str = ""


def evaluation_card(job: EvaluationJob) -> dict[str, Any]:
    titles = {
        "queued": "评测任务已创建",
        "running": "评测进行中",
        "publishing": "评测完成，正在发布报告",
        "succeeded": "评测完成，报告已发布",
        "publish_failed": "评测完成，报告发布失败",
        "failed": "评测执行失败",
        "interrupted": "评测任务已中断",
    }
    text = f"**任务编号**：{job.job_id}\n**创建时间**：{job.created_at}\n"
    if job.summary:
        data = job.summary
        text += (
            f"\n**自动规则通过**：{data['passed_cases']}/{data['total_cases']}\n"
            f"**执行错误轮次**：{data['turn_counts']['error']}\n"
            f"**已记录轮次耗时**：{data['duration_seconds']} 秒\n"
            "答案质量仍待人工复核，自动通过率不代表答案正确率。\n"
        )
        if data["turn_counts"]["error"]:
            text += "\n部分轮次执行失败，详情和已完成结果保留在报告中。\n"
    if job.status == "publish_failed":
        text += "\n本地报告已保留，发布失败不会删除评测结果。\n"
    if job.status in {"failed", "publish_failed", "interrupted"}:
        text += "\n请在后台核对任务记录中的失败阶段和原因。\n"
    if job.notification_error:
        text += "\n此前完成通知未发送成功，当前显示的是已保存的任务状态。\n"
    title = titles[job.status]
    if job.status == "succeeded" and job.summary.get("turn_counts", {}).get("error"):
        title = "评测完成（含执行错误），报告已发布"
    card = notice_card(title, text)
    elements = card["body"]["elements"]
    if job.report_url:
        elements.append(
            {
                "tag": "button",
                "type": "primary",
                "text": {"tag": "plain_text", "content": "打开评测报告"},
                "behaviors": [{"type": "open_url", "default_url": job.report_url}],
            }
        )
    elements.append(
        {
            "tag": "button",
            "type": "default",
            "text": {"tag": "plain_text", "content": "查询此任务状态"},
            "behaviors": [
                {"type": "callback", "value": {"action": "eval_status", "job_id": job.job_id}}
            ],
        }
    )
    return card


class FeishuEvaluationUI:
    def __init__(
        self,
        store: EvaluationJobStore,
        worker: EvaluationWorker,
        cases_path: Path,
        output_root: Path,
        send_card: Callable[[str, str, dict[str, Any], str], tuple[str, str]],
        allowed_open_ids: set[str] | None = None,
    ):
        self.store, self.worker, self.cases_path = store, worker, cases_path
        self.output_root, self.send_card = output_root, send_card
        self.allowed_open_ids = allowed_open_ids or set()

    def handle(self, action: EvaluationAction) -> None:
        try:
            if self.allowed_open_ids and action.open_id not in self.allowed_open_ids:
                raise ValueError("当前用户未被配置为评测操作人")
            if action.action == "eval_start":
                job, created = self.store.create(
                    action.event_id, action.open_id, load_suite(self.cases_path), self.output_root
                )
                self.worker.wake.set()
                card = evaluation_card(self.store.get(job.job_id, action.open_id))
                if not created:
                    card["body"]["elements"].insert(
                        0, {"tag": "markdown", "content": "已找到现有任务，本次未重复启动。"}
                    )
            else:
                job = (
                    self.store.get(action.job_id, action.open_id)
                    if action.job_id
                    else self.store.latest(action.open_id)
                )
                card = (
                    evaluation_card(job) if job else notice_card("评测状态", "你还没有发起过评测。")
                )
        except (OSError, ValueError, sqlite3.Error) as exc:
            logger.exception("评测菜单操作失败")
            message = (
                str(exc)
                if isinstance(exc, (JobBusyError, ValueError))
                else "无法读取评测配置或任务记录，请检查后台日志。"
            )
            card = notice_card("评测操作未完成", message)
        self.send_card("open_id", action.open_id, card, f"evaluation-action:{action.event_id}")
