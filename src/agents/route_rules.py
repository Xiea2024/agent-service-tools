"""确定性意图规则：在调用模型之前判断这轮问题是否需要检索资料。

Iter 1 只把判断结果记录成 route 阶段，不改变图的走向；Iter 2 的 router 节点
会消费同一个函数，把“规则优先、模型兜底”变成真实的分流，避免为寒暄和
纯澄清问题付出一次模型调用。

这里刻意使用关键词而不是模型：
- 成本为零，且结果可复现，适合作为归因的对照基线；
- 关键词命中与否本身就是一条可观测信号（route.matched_rule）；
- 规则判断不了的问题记为 general，交给模型，不假装已经分类成功。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 需要文档依据的说法：故障现象、知识库、配置与版本差异。
RETRIEVAL_HINTS: tuple[str, ...] = (
    "文档",
    "知识库",
    "资料",
    "手册",
    "指南",
    "配置",
    "启动",
    "报错",
    "异常",
    "超时",
    "失败",
    "跨域",
    "状态码",
    "版本",
    "依赖",
    "日志",
    "堆栈",
    "traceback",
    "request_id",
)

# 明确的寒暄与确认，命中则不需要检索。
SMALLTALK: tuple[str, ...] = (
    "你好",
    "您好",
    "谢谢",
    "在吗",
    "早上好",
    "下午好",
    "晚上好",
)

_LOG_SIGNAL = re.compile(r"(?:\b5\d{2}\b|traceback|exception|stack\s*trace)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """一次路由判断的结果；字段都可直接进指标，不含原文。"""

    intent: str
    needs_retrieval: bool
    matched_rule: str

    def as_decisions(self) -> dict[str, object]:
        return {
            "intent": self.intent,
            "needs_retrieval": self.needs_retrieval,
            "matched_rule": self.matched_rule,
        }


def classify_intent(text: str) -> RouteDecision:
    """把一句话分成 doc_qa / log_diag / general，并判断是否需要检索。"""
    normalized = " ".join((text or "").split())
    if not normalized:
        return RouteDecision("general", False, "empty")
    lowered = normalized.lower()

    if any(word in normalized for word in SMALLTALK) and len(normalized) <= 12:
        return RouteDecision("general", False, "smalltalk")
    if _LOG_SIGNAL.search(normalized):
        return RouteDecision("log_diag", True, "log_signal")
    hits = [word for word in RETRIEVAL_HINTS if word.lower() in lowered]
    if hits:
        intent = "log_diag" if "日志" in hits or "堆栈" in hits else "doc_qa"
        return RouteDecision(intent, True, f"keyword:{hits[0]}")
    return RouteDecision("general", False, "no_hint")
