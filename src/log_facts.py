"""日志事实解析：用确定性规则把一段日志变成可引用的事实，再交给模型解释。

约定：解析器只输出它能确定的东西。缺失字段、格式不符、同一字段出现多个值，
都记成 warning 返回，不猜、不补、不把“没解析到”当成“日志里没有”。
模型看到的是结构化事实，不是原始日志全文，因此它不能凭空编造 request_id
或状态码——这正是把确定性程序和 LLM 分工的理由。

本模块不做行级全文索引，也不尝试识别所有日志格式；首版只覆盖 Java/Python
常见的几种堆栈与控制台输出。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MAX_CHARS = 200_000
MAX_LINES = 2_000
MAX_FRAMES = 5
MAX_WARNINGS = 8

_TIMESTAMP = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})?)"
)
_LEVEL = re.compile(r"\b(TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|CRITICAL)\b")
_EXCEPTION = re.compile(r"\b([A-Z][A-Za-z0-9_]*(?:Exception|Error|Throwable))\b")
_REQUEST_ID = re.compile(
    r"\b(?:request[_-]?id|req[_-]?id|trace[_-]?id|correlation[_-]?id|x-request-id)"
    r"\s*[:=]\s*[\"']?([A-Za-z0-9_-]{6,64})",
    re.IGNORECASE,
)
_STATUS = re.compile(
    r'(?:\bHTTP/[\d.]+\S*\s+|[?&"]?(?:status|status[_-]?code|code)["\s:=]+)([1-5]\d{2})\b',
    re.IGNORECASE,
)
_FRAME = re.compile(r'File "([^"]+)", line (\d+), in (\S+)')
_JAVA_FRAME = re.compile(r"\bat ([\w$.]+)\(([\w$]+\.java):(\d+)\)")


@dataclass(frozen=True, slots=True)
class LogFacts:
    """解析结果；facts 全部来自正则，warnings 说明为什么某些字段缺失。"""

    facts: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    line_count: int = 0
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "facts": dict(self.facts),
            "warnings": list(self.warnings),
            "line_count": self.line_count,
            "truncated": self.truncated,
        }

    def as_prompt_block(self) -> str:
        """序列化成给模型的文本块；空结果也明确写出，避免模型误以为“一切正常”。"""
        if not self.facts:
            body = "未解析出任何结构化事实。"
        else:
            body = "\n".join(f"- {key}: {value}" for key, value in self.facts.items())
        warnings = "\n".join(f"- {warning}" for warning in self.warnings) or "- 无"
        return (
            "以下内容是程序正则解析得到的确定事实，不是模型推断：\n"
            f"{body}\n\n解析警告：\n{warnings}"
        )


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group(1) if match else None


def parse_log_facts(text: str) -> LogFacts:
    """解析日志文本；永远返回结果对象，格式完全不符时用 warnings 表达。"""
    raw = text or ""
    truncated = len(raw) > MAX_CHARS
    clipped = raw[:MAX_CHARS]
    lines = clipped.splitlines()
    if len(lines) > MAX_LINES:
        clipped = "\n".join(lines[:MAX_LINES])
        lines = lines[:MAX_LINES]
        truncated = True

    facts: dict[str, Any] = {}
    warnings: list[str] = []

    if not clipped.strip():
        return LogFacts(warnings=["日志为空，没有任何可解析内容"], truncated=truncated)

    timestamp = _first(_TIMESTAMP, clipped)
    if timestamp:
        facts["timestamp"] = timestamp
    else:
        warnings.append("未识别到时间戳；不要假设故障发生时间")

    level = _first(_LEVEL, clipped)
    if level:
        facts["level"] = level.upper().replace("WARNING", "WARN")
    else:
        warnings.append("未识别到日志级别")

    exception = _first(_EXCEPTION, clipped)
    if exception:
        facts["exception"] = exception

    request_ids = list(dict.fromkeys(_REQUEST_ID.findall(clipped)))
    if request_ids:
        facts["request_id"] = request_ids[0]
        if len(request_ids) > 1:
            warnings.append(f"识别到 {len(request_ids)} 个 request_id，只采用第一个")

    status = _first(_STATUS, clipped)
    if status:
        facts["status_code"] = int(status)

    frames = [f"{match.group(1)}:{match.group(2)}" for match in _FRAME.finditer(clipped)]
    if not frames:
        frames = [
            f"{match.group(1)}({match.group(2)}:{match.group(3)})"
            for match in _JAVA_FRAME.finditer(clipped)
        ]
    if frames:
        facts["stack_frames"] = frames[:MAX_FRAMES]

    if "exception" not in facts and "status_code" not in facts:
        warnings.append("未识别到异常类型或 HTTP 状态码；不能据此判断根因")
    if not any(key in facts for key in ("exception", "status_code", "stack_frames")):
        warnings.append("这段文本可能不是日志，建议让用户补充报错原文")

    facts["line_count"] = len(lines)
    if truncated:
        warnings.append(f"日志过长，只解析前 {MAX_CHARS} 字符 / {MAX_LINES} 行")

    return LogFacts(
        facts=facts,
        warnings=warnings[:MAX_WARNINGS],
        line_count=len(lines),
        truncated=truncated,
    )
