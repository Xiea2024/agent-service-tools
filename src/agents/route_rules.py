"""确定性意图规则：给 supervisor 兜底，不是主路由。

真正的路由由 LLM supervisor 决定（见 agents/supervisor.py）。这个模块只在
模型没给出合法委派时兜底，所以它的输出会标成 ``decided_by=rules_fallback``，
不会被当成“模型判断”。

区分「提到 500」和「贴了一段日志」很重要：只有出现堆栈、级别+时间戳、
request_id 这类**日志结构**才进日志分支，否则一句“订单接口返回 500”
会被误判成日志诊断。
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

# 日志结构特征：出现任意一条才认为用户贴的是日志。裸的 “500” 不算。
_LOG_SIGNAL = re.compile(
    r"(?im)"
    r"traceback \(most recent call last\)"
    r"|file \"[^\"]+\", line \d+"
    r"|\bat [\w$.]+\([\w$]+\.java:\d+\)"
    r"|\b(?:trace|debug|info|warn|warning|error|fatal|critical)\b\s*[\d:\[]"
    r"|(?:request[_-]?id|trace[_-]?id)\s*[:=]"
)
_TIMESTAMP = re.compile(r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")
_LEVEL_TOKEN = re.compile(r"\b(?:ERROR|FATAL|WARN|WARNING)\b")


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """一次规则判断的结果；字段都可直接进指标，不含原文。"""

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
    """把一轮输入分成 doc_qa / log_diag / general。"""
    normalized = " ".join((text or "").split())
    if not normalized:
        return RouteDecision("general", False, "empty")
    lowered = normalized.lower()

    if any(word in normalized for word in SMALLTALK) and len(normalized) <= 12:
        return RouteDecision("general", False, "smalltalk")

    # 先判日志：贴了日志的人通常同时提到“报错”，不能让关键词规则抢占。
    if _LOG_SIGNAL.search(normalized) or (
        _TIMESTAMP.search(normalized) and _LEVEL_TOKEN.search(normalized)
    ):
        return RouteDecision("log_diag", False, "log_structure")

    hits = [word for word in RETRIEVAL_HINTS if word.lower() in lowered]
    if hits:
        intent = "log_diag" if "日志" in hits or "堆栈" in hits else "doc_qa"
        return RouteDecision(intent, intent == "doc_qa", f"keyword:{hits[0]}")
    return RouteDecision("general", False, "no_hint")
