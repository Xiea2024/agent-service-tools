"""失败归因：把一轮问答的失败定位到 route/retrieve/evidence/generate/verify。

设计原则：

1. **按流水线顺序取第一个失败点**。检索没命中时，后面的“回答没引用”只是结果，
   不是原因；先修最早的那一步才有意义。
2. **只用确定性证据**。输入是评测器已算出的 checks、工具证据和服务端阶段记录，
   不调用模型判断，因此同一份输入永远得到同一个归因，可以进 CI 比较。
3. **无法归因就明说**。规则命中不了时返回 ``unattributed``，并把失败检查名列出来，
   不硬塞一个最像的阶段。

这里的“阶段”和 observability.stages 中的固定枚举一一对应，指标、trace、
评测报告使用的是同一套名字。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 只为类型标注导入，避免与 evaluation 形成循环依赖
    from evaluation import Turn

STAGE_ORDER: tuple[str, ...] = ("route", "retrieve", "evidence", "generate", "verify")

# 这些检查失败说明“回答本身的可靠性”出了问题，而不是检索或引用的问题。
_VERIFY_CHECKS = (
    "run_id_consistent",
    "tool_calls_matched",
    "tool_budget",
    "known_tools",
    "tool_payload",
)

_URL = re.compile(r"https?://[^\s<>\]\)\"']+")


def _urls(text: str) -> set[str]:
    return {value.rstrip(".,;，。；") for value in _URL.findall(text or "")}


def _failed(result: dict[str, Any]) -> set[str]:
    return {check["name"] for check in result.get("checks") or [] if not check["passed"]}


def _by_stage(stages: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in stages:
        grouped.setdefault(str(record.get("stage")), []).append(record)
    return grouped


def _tool_calls(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        call for message in result.get("messages") or [] for call in message.get("tool_calls") or []
    ]


def _hits(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [hit for item in result.get("evidence") or [] for hit in item.get("hits") or []]


def _verdict(
    stage: str, reason: str, failed: set[str], stages: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "stage": stage,
        "reason": reason,
        "failed_checks": sorted(failed),
        "variant": next(
            (record.get("variant") for record in stages if record.get("variant")), None
        ),
    }


def attribute(turn: Turn, result: dict[str, Any]) -> dict[str, Any]:
    """返回 ``{stage, reason, failed_checks, variant}``；通过时为 stage=none。"""
    stages = list(result.get("stages") or [])
    grouped = _by_stage(stages)
    failed = _failed(result)
    calls = _tool_calls(result)
    hits = _hits(result)
    answer = result.get("answer") or ""

    if not failed and result.get("status") == "passed":
        return _verdict("none", "全部自动检查通过", failed, stages)

    # 1. route：需要检索却没检索，或明确不需要却检索了。确定性分类器只是旁证。
    routed = grouped.get("route") or []
    intent = routed[0]["decisions"].get("intent") if routed else None
    if turn.retrieval == "required" and not calls:
        return _verdict(
            "route",
            f"案例要求检索，本轮没有调用工具（规则判断 intent={intent or '未知'}）",
            failed,
            stages,
        )
    if turn.retrieval == "forbidden" and calls:
        return _verdict(
            "route",
            f"案例要求不检索，本轮调用了 {len(calls)} 次工具（规则判断 intent={intent or '未知'}）",
            failed,
            stages,
        )

    # 2. retrieve：调用失败、命中为空、或没命中应命中的文档。
    retrieved = grouped.get("retrieve") or []
    broken = [r for r in retrieved if r.get("outcome") in {"error", "timeout"}]
    if broken:
        code = broken[0].get("decisions", {}).get("error_code") or broken[0]["outcome"]
        return _verdict("retrieve", f"检索失败（{code}）", failed, stages)
    if failed & {"retrieval_available", "expected_retrieval_status", "expected_documents"}:
        return _verdict(
            "retrieve",
            f"检索结果不满足案例预期（命中 {len(hits)} 段）",
            failed,
            stages,
        )
    if turn.retrieval == "required" and calls and not hits:
        return _verdict("retrieve", "调用了检索但没有任何命中", failed, stages)

    # 3. evidence：有命中却没被引用，或引用了没有出处的链接。
    cited = sum(
        int(r.get("decisions", {}).get("cited") or 0) for r in grouped.get("evidence") or []
    )
    if hits and not cited:
        return _verdict("evidence", f"命中 {len(hits)} 段但没有被回答引用", failed, stages)
    if "source_urls" in failed:
        allowed = (
            set().union(*(_urls(hit.get("source_url", "")) for hit in hits)) if hits else set()
        )
        unsupported = _urls(answer) - allowed
        return _verdict(
            "evidence", f"回答中出现 {len(unsupported)} 个没有证据支持的链接", failed, stages
        )

    # 4. generate：没有产出可用回答，或模型调用超时/预算耗尽。
    generated = grouped.get("generate") or []
    if any(r.get("outcome") in {"timeout", "budget_exhausted"} for r in generated):
        return _verdict("generate", "模型调用超时或预算耗尽", failed, stages)
    if result.get("error"):
        return _verdict(
            "generate", f"执行错误：{result['error'].get('type', 'unknown')}", failed, stages
        )
    if failed & {"answer_present", "agent_completed"}:
        return _verdict("generate", "没有产出可用的最终回答", failed, stages)

    # 5. verify：回答与协议一致性（run_id、工具配对、轮次、工具白名单）。
    if failed & set(_VERIFY_CHECKS):
        return _verdict("verify", "回答与消息协议不一致", failed, stages)

    return _verdict(
        "unattributed",
        "检查未通过，但现有规则无法归因到具体阶段",
        failed,
        stages,
    )


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """按阶段统计失败次数，供 summary.json 与看板使用；通过的不计入。"""
    stages = Counter(
        (result.get("attribution") or {}).get("stage")
        for result in results
        if (result.get("attribution") or {}).get("stage") not in {None, "none"}
    )
    return {
        "stage_failures": dict(stages),
        "unattributed_turns": stages.get("unattributed", 0),
    }
