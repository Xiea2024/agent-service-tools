"""Run fixed cases through AgentClient and retain evidence for local or Feishu reports."""

import asyncio
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from client import AgentClient
from harness.attribution import STAGE_ORDER, attribute
from harness.attribution import summarize as summarize_attribution
from schema import ChatMessage


def timestamp() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


class Turn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1)
    rubric: list[str] = Field(min_length=1)
    retrieval: Literal["required", "forbidden", "optional"] = "optional"
    expected_document_ids: list[str] = Field(default_factory=list)
    expected_status: Literal["ok", "no_results", "error"] | None = None

    @field_validator("message")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("问题不能只有空白")
        return value


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1)
    category: str = Field(min_length=1)
    turns: list[Turn] = Field(min_length=1)


class Suite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    id: str
    version: str
    description: str
    cases: list[Case] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_case_ids(self) -> "Suite":
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("案例 ID 不能重复")
        return self


def load_suite(path: Path, case_ids: list[str] | None = None) -> Suite:
    suite = Suite.model_validate_json(path.read_text(encoding="utf-8-sig"))
    if case_ids:
        unknown = set(case_ids) - {case.id for case in suite.cases}
        if unknown:
            raise ValueError(f"未知案例 ID：{', '.join(sorted(unknown))}")
        suite = suite.model_copy(update={"cases": [c for c in suite.cases if c.id in case_ids]})
    return suite


def urls(text: str) -> set[str]:
    return {value.rstrip(".,;，。；") for value in re.findall(r"https?://[^\s<>\]\)\"']+", text)}


def check_turn(
    turn: Turn, messages: list[ChatMessage], allowed_urls: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": passed, "detail": detail})

    calls = [call for message in messages for call in message.tool_calls]
    call_ids = [call["id"] for call in calls]
    tool_messages = [message for message in messages if message.type == "tool"]
    result_ids = [message.tool_call_id for message in tool_messages]
    final = messages[-1] if messages and messages[-1].type == "ai" else None
    answer = final.content if final and not final.tool_calls else ""
    check("answer_present", bool(answer.strip()), "最后一条消息应为非空、无工具请求的 AI 回答")
    run_ids = {message.run_id for message in messages if message.run_id}
    check(
        "run_id_consistent",
        len(run_ids) == 1 and all(message.run_id for message in messages),
        "本轮完整消息应携带同一个 run_id",
    )
    check(
        "tool_calls_matched",
        bool(all(call_ids))
        and len(set(call_ids)) == len(call_ids)
        and Counter(call_ids) == Counter(result_ids),
        "每个工具请求应有且只有一个匹配的执行结果",
    )
    check(
        "agent_completed",
        answer
        not in {
            "本轮处理已超时，暂时无法完成回答，请稍后重试。",
            "本轮模型响应超时，请稍后重试。",
            "已达到本轮检索预算，请缩小问题范围后继续；当前无法确认结论。",
        },
        "未返回当前 Agent 的超时或预算耗尽兜底文本",
    )
    check("tool_budget", len(calls) <= 2, f"本轮工具调用 {len(calls)} 次，上限 2 次")
    check(
        "known_tools",
        all(call["name"] == "search_feishu_documents" for call in calls),
        "当前业务只绑定 search_feishu_documents",
    )
    if turn.retrieval != "optional":
        check(
            "retrieval_policy",
            bool(calls) if turn.retrieval == "required" else not calls,
            f"案例要求：{turn.retrieval}",
        )

    evidence: list[dict[str, Any]] = []
    for message in tool_messages:
        try:
            payload = json.loads(message.content)
            if not isinstance(payload, dict) or payload.get("status") not in {
                "ok",
                "no_results",
                "error",
            }:
                raise ValueError("未知工具返回格式")
            hits = payload.get("hits")
            if not isinstance(hits, list) or any(not isinstance(hit, dict) for hit in hits):
                raise ValueError("hits 必须为对象列表")
            if (payload["status"] == "ok") != bool(hits):
                raise ValueError("status 与 hits 不一致")
            fields = ("chunk_id", "document_id", "title", "source_url", "content")
            if any(not isinstance(hit.get(key), str) for hit in hits for key in fields):
                raise ValueError("命中片段缺少必需的文本字段")
            evidence.append({"tool_call_id": message.tool_call_id, **payload})
        except (ValueError, TypeError) as exc:
            check("tool_payload", False, f"工具结果无法解析：{exc}")
    statuses = [item["status"] for item in evidence]
    check(
        "retrieval_available",
        "error" not in statuses or turn.expected_status == "error",
        f"实际检索状态：{statuses or '未检索'}",
    )
    if turn.expected_status:
        check(
            "expected_retrieval_status",
            bool(statuses) and all(status == turn.expected_status for status in statuses),
            f"预期 {turn.expected_status}；实际 {statuses}",
        )
    hits = [hit for item in evidence for hit in item["hits"]]
    if turn.expected_document_ids:
        actual = {hit.get("document_id") for hit in hits}
        check(
            "expected_documents",
            set(turn.expected_document_ids).issubset(actual),
            f"预期文档 {turn.expected_document_ids}；实际 {sorted(str(v) for v in actual)}",
        )
    if hits and (turn.retrieval == "required" or turn.expected_document_ids):
        # 案例要求证据时，“命中但没引用”是一个独立失败点：它不是检索问题，
        # 也不是生成问题，而是证据到回答之间的断裂，归因时单独记为 evidence。
        cited = urls(answer) & {hit.get("source_url") for hit in hits}
        check(
            "evidence_cited",
            bool(cited),
            f"命中 {len(hits)} 段，回答引用了其中 {len(cited)} 个来源",
        )
    allowed_urls.update(hit["source_url"] for hit in hits)
    unsupported = urls(answer) - allowed_urls
    check("source_urls", not unsupported, f"没有证据支持的链接：{sorted(unsupported)}")
    return checks, evidence, answer


async def evaluate_turn(
    client: AgentClient,
    turn: Turn,
    thread_id: str,
    user_id: str,
    model: str | None,
    timeout: float,
    allowed_urls: set[str],
) -> dict[str, Any]:
    started = perf_counter()
    started_at = timestamp()
    messages: list[ChatMessage] = []
    error = None
    allowed_urls.update(urls(turn.message))
    try:
        async with asyncio.timeout(timeout):
            async for message in client.astream(
                turn.message, model=model, thread_id=thread_id, user_id=user_id, stream_tokens=False
            ):
                if not isinstance(message, ChatMessage):
                    raise ValueError("关闭 token 流后仍收到非完整消息")
                messages.append(message)
                if (
                    message.type == "ai"
                    and message.content.startswith("Error: ")
                    and not message.run_id
                ):
                    raise RuntimeError(message.content)
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
    checks, evidence, answer = check_turn(turn, messages, allowed_urls)
    run_id = next((message.run_id for message in messages if message.run_id), None)
    # 观测缺失不阻塞评测：旧服务版本或进程重启后这里会拿到空列表。
    fetch_stages = getattr(client, "astages", None)
    fetched = await fetch_stages(run_id) if run_id and fetch_stages else []
    stages = [record for record in fetched or [] if isinstance(record, dict)]
    result = {
        "message": turn.message,
        "thread_id": thread_id,
        "run_id": run_id,
        "started_at": started_at,
        "duration_seconds": round(perf_counter() - started, 3),
        "status": "error" if error else "passed" if all(c["passed"] for c in checks) else "failed",
        "answer": answer,
        "messages": [message.model_dump() for message in messages],
        "evidence": evidence,
        "checks": checks,
        "stages": stages,
        "error": error,
        "quality_review": {"status": "pending", "rubric": turn.rubric, "notes": ""},
    }
    # 归因只读上面这份结果，不调用模型，因此同一份输入永远得到同一个结论。
    result["attribution"] = attribute(turn, result)
    return result


def save_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def summary(manifest: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {
        status: sum(r["status"] == status for r in results)
        for status in ["passed", "failed", "error"]
    }
    case_ids = {r["case_id"] for r in results}
    passed_cases = sum(
        all(r["status"] == "passed" for r in results if r["case_id"] == case_id)
        and sum(r["case_id"] == case_id for r in results) == manifest["case_turn_counts"][case_id]
        for case_id in case_ids
    )
    return {
        **manifest,
        "recorded_turns": len(results),
        "unrecorded_turns": manifest["total_turns"] - len(results),
        "turn_counts": counts,
        "passed_cases": passed_cases,
        "case_pass_rate": passed_cases / manifest["total_cases"],
        "quality_review_pending": len(results),
        "duration_seconds": round(sum(r["duration_seconds"] for r in results), 3),
        # 阶段归因：失败轮次落在 route/retrieve/evidence/generate/verify 的哪一步。
        **summarize_attribution(results),
    }


def fenced(text: str) -> str:
    fence = "`" * max(3, max((len(m) + 1 for m in re.findall(r"`+", text)), default=3))
    return f"{fence}text\n{text}\n{fence}"


def write_report(output_dir: Path, manifest: dict[str, Any], results: list[dict[str, Any]]) -> None:
    data = summary(manifest, results)
    save_json(output_dir / "summary.json", data)
    lines = [
        "# SupportPilot 本地评测报告",
        "",
        f"- 评测编号：{manifest['evaluation_id']}",
        f"- 案例集：{manifest['suite_id']} / {manifest['suite_version']}",
        f"- 状态：{manifest['status']}；时间采用 UTC+08:00",
        f"- Agent：{manifest['agent_id']}；请求模型：{manifest['requested_model'] or '服务默认模型'}",
        f"- 案例规则通过：{data['passed_cases']}/{manifest['total_cases']}"
        f"（{data['case_pass_rate']:.1%}）",
        f"- 已记录轮次：{len(results)}/{manifest['total_turns']}；"
        f"通过 {data['turn_counts']['passed']}，未通过 {data['turn_counts']['failed']}，"
        f"执行错误 {data['turn_counts']['error']}",
        f"- 已记录轮次总耗时：{data['duration_seconds']} 秒",
        "- 答案质量：待人工复核。自动规则通过率不代表答案正确率。",
        "- 原始数据：results.jsonl；案例快照：cases.json；运行信息：manifest.json。",
        "",
        "## 失败归因",
        "",
        "按流水线顺序取第一个失败点：route → retrieve → evidence → generate → verify。"
        "归因只用确定性证据，不调用模型判断；无法归因的轮次单独记为 unattributed。",
        "",
    ]
    stage_failures = data["stage_failures"]
    if not stage_failures:
        lines.append("- 本轮没有被归因到具体阶段的失败轮次。")
    else:
        lines.append("| 阶段 | 失败轮次 |")
        lines.append("| --- | --- |")
        for name in STAGE_ORDER + ("unattributed",):
            if stage_failures.get(name):
                lines.append(f"| {name} | {stage_failures[name]} |")
    lines.extend(["", "## 结果明细", ""])
    for result in results:
        lines.extend(
            [
                f"### {result['case_id']} · {result['title']} · 第 {result['turn_index']} 轮",
                "",
                f"状态：{result['status']}；耗时：{result['duration_seconds']} 秒",
                f"归因：{result.get('attribution', {}).get('stage', '未记录')}"
                f" · {result.get('attribution', {}).get('reason', '')}",
                f"thread_id：{result['thread_id']}",
                f"run_id：{result['run_id'] or '未返回'}",
                "",
                "#### 问题",
                "",
                fenced(result["message"]),
                "",
                "#### 回答",
                "",
                fenced(result["answer"] or "（无最终回答）"),
                "",
                "#### 自动检查",
                "",
            ]
        )
        for check in result["checks"]:
            lines.append(
                f"- {'通过' if check['passed'] else '未通过'} · {check['name']}：{check['detail']}"
            )
        if result["error"]:
            lines.extend(
                ["", "执行错误：", "", fenced(json.dumps(result["error"], ensure_ascii=False))]
            )
        lines.extend(["", "#### 工具调用和检索证据", ""])
        trace = {
            "tool_calls": [
                call for message in result["messages"] for call in message["tool_calls"]
            ],
            "tool_results": [
                message for message in result["messages"] if message["type"] == "tool"
            ],
            "evidence": result["evidence"],
        }
        lines.extend(
            [
                fenced(json.dumps(trace, ensure_ascii=False, indent=2)),
                "",
                "#### 人工复核（待完成）",
                "",
            ]
        )
        lines.extend(f"- [ ] {criterion}" for criterion in result["quality_review"]["rubric"])
        lines.extend(["", "复核结论与理由：待填写。", ""])
    (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


async def run_evaluation(
    suite: Suite,
    client: AgentClient,
    output_dir: Path,
    model: str | None = None,
    timeout: float = 90,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=False)
    snapshot = suite.model_dump()
    manifest = {
        "schema_version": 1,
        "evaluation_id": str(uuid4()),
        "suite_id": suite.id,
        "suite_version": suite.version,
        "cases_sha256": hashlib.sha256(
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "agent_id": client.agent,
        "base_url": client.base_url,
        "requested_model": model,
        "timeout_seconds": timeout,
        "started_at": timestamp(),
        "status": "running",
        "total_cases": len(suite.cases),
        "total_turns": sum(len(case.turns) for case in suite.cases),
        "case_turn_counts": {case.id: len(case.turns) for case in suite.cases},
    }
    save_json(output_dir / "cases.json", snapshot)
    save_json(output_dir / "manifest.json", manifest)
    results: list[dict[str, Any]] = []
    write_report(output_dir, manifest, results)
    try:
        with (output_dir / "results.jsonl").open("x", encoding="utf-8") as stream:
            for case in suite.cases:
                thread_id = str(uuid4())
                allowed_urls: set[str] = set()
                for index, turn in enumerate(case.turns, start=1):
                    result = await evaluate_turn(
                        client,
                        turn,
                        thread_id,
                        f"eval:{manifest['evaluation_id']}",
                        model,
                        timeout,
                        allowed_urls,
                    )
                    result.update(
                        {
                            "evaluation_id": manifest["evaluation_id"],
                            "case_id": case.id,
                            "title": case.title,
                            "category": case.category,
                            "turn_index": index,
                        }
                    )
                    stream.write(json.dumps(result, ensure_ascii=False) + "\n")
                    stream.flush()
                    results.append(result)
                    write_report(output_dir, manifest, results)
                    print(f"{case.id} [{index}/{len(case.turns)}] {result['status']}", flush=True)
                    if result["error"]:
                        print(
                            f"  {result['error']['type']}: {result['error']['message']}", flush=True
                        )
                    if result["status"] == "error":
                        break
        manifest["status"] = "completed"
    except BaseException:
        manifest["status"] = "interrupted"
        raise
    finally:
        manifest["finished_at"] = timestamp()
        save_json(output_dir / "manifest.json", manifest)
        write_report(output_dir, manifest, results)
    return summary(manifest, results)
