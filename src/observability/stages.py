"""按阶段归因一轮问答：同一套 stage 名称驱动指标、调用追踪和评测结果。

一轮问答被拆成固定的几步，每一步只记录低基数、无正文的决策字段。这样
“哪一步不准”可以由数据回答，而不是靠事后翻日志推测：

    route    提示词之外的第一道判断：这个问题是否需要检索
    memory   长期记忆召回（Iter 2 接入，本版不产生记录）
    retrieve 查询向量、Top-K、阈值过滤
    evidence 命中片段是否真的被回答引用
    generate 模型调用（模型名、工具请求数、输出长度）
    verify   回答自检（无依据链接、预算耗尽）

本模块只做记录，不改变图的执行路径：即使摘掉全部 stage 调用，Agent 的回答
行为也不应变化。记录保存在进程内的有界字典里，供 ``GET /runs/{run_id}/stages``
读取；单进程单 Worker 的部署假设与项目其他部分一致，多副本下读到空记录是
预期结果，不影响主链路。
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict, deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from observability.metrics import STAGE_CALLS, STAGE_DURATION

logger = logging.getLogger(__name__)

# 固定枚举：新增阶段必须改这里，避免调用点拼错名字后指标静默分裂。
ROUTE = "route"
MEMORY = "memory"
RETRIEVE = "retrieve"
EVIDENCE = "evidence"
GENERATE = "generate"
VERIFY = "verify"
STAGES: tuple[str, ...] = (ROUTE, MEMORY, RETRIEVE, EVIDENCE, GENERATE, VERIFY)

# outcome 同样固定，避免把异常消息、URL、片段 ID 写进标签造成高基数。
OUTCOMES: tuple[str, ...] = (
    "success",
    "no_results",
    "unused_hits",
    "unsupported_citation",
    "budget_exhausted",
    "timeout",
    "error",
    "skipped",
)

# 有界：只保留最近若干次运行，每次运行最多若干条记录，避免长时间运行撑爆内存。
MAX_RUNS = 200
MAX_RECORDS_PER_RUN = 12
MAX_RUN_ID_CHARS = 64
MAX_DECISIONS = 8
MAX_STR_CHARS = 64

_DECISION_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_VARIANT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
_UNBOUND = "_unbound"


@dataclass(frozen=True, slots=True)
class StageRecord:
    """一次阶段执行的记录；只有数字、布尔、枚举和短字符串。"""

    stage: str
    outcome: str
    duration_seconds: float
    variant: str
    decisions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "outcome": self.outcome,
            "duration_seconds": self.duration_seconds,
            "variant": self.variant,
            "decisions": dict(self.decisions),
        }


_RUNS: OrderedDict[str, deque[StageRecord]] = OrderedDict()


def _sanitize(key: Any, value: Any) -> tuple[str, Any] | None:
    """丢弃无法保证低基数的决策字段，而不是把它们截断后照发。"""
    if not isinstance(key, str) or not _DECISION_KEY.fullmatch(key):
        return None
    if isinstance(value, bool) or isinstance(value, int):
        return key, value
    if isinstance(value, float):
        finite = value == value and abs(value) != float("inf")
        return (key, round(value, 4)) if finite else None
    if isinstance(value, str):
        return key, " ".join(value.split())[:MAX_STR_CHARS]
    return None


def _resolve_variant(explicit: str | None) -> str:
    candidate = str(explicit if explicit else _configured_variant())
    return candidate if _VARIANT.fullmatch(candidate) else "v1"


def _configured_variant() -> str:
    # 延迟导入：observability 被 agents/core 依赖，模块级导入 settings 会形成环。
    try:
        from core import settings

        return str(getattr(settings, "AGENT_VARIANT", "v1"))
    except Exception:  # noqa: BLE001 - 观测失败不能影响业务调用
        return "v1"


def run_id_of(config: Any) -> str:
    """从 RunnableConfig 取本次请求的标识；没有则归入无绑定桶。

    优先读 ``metadata.run_id``：LangGraph 会给每个节点生成自己的 run_id，节点里读
    ``config["run_id"]`` 拿到的是节点级标识，按它分组会把一轮问答拆成多条记录。
    ``metadata`` 由服务在构造 RunnableConfig 时写入并原样下发到子图，是唯一稳定的来源。
    """
    if isinstance(config, dict):
        metadata = config.get("metadata") or {}
        candidates = (
            metadata.get("run_id") if isinstance(metadata, dict) else None,
            (config.get("configurable") or {}).get("run_id"),
            config.get("run_id"),
        )
        for candidate in candidates:
            if candidate:
                return str(candidate)[:MAX_RUN_ID_CHARS]
    return _UNBOUND


@contextmanager
def stage(name: str, *, config: Any = None, variant: str | None = None) -> Iterator[dict[str, Any]]:
    """记录一个阶段。用法与 ``metrics.operation`` 一致，``outcome`` 是保留键。

    >>> with stage(RETRIEVE, config=config) as observed:
    ...     hits = search()
    ...     observed["hits"] = len(hits)
    ...     observed["outcome"] = "success" if hits else "no_results"

    异常不会被吞掉；超时和异常分别记为 timeout / error 后原样抛出。
    """
    if name not in STAGES:
        raise ValueError(f"unknown stage: {name}")
    run_id = run_id_of(config)
    started = perf_counter()
    values: dict[str, Any] = {}
    try:
        yield values
    except TimeoutError:
        values["outcome"] = "timeout"
        raise
    except BaseException:
        values["outcome"] = "error"
        raise
    finally:
        _emit(name, run_id, values, perf_counter() - started, variant)


def _emit(
    name: str, run_id: str, values: dict[str, Any], elapsed: float, variant: str | None
) -> None:
    outcome = values.pop("outcome", "success")
    if outcome not in OUTCOMES:
        logger.warning("未知阶段结果 %r，按 error 记录", outcome)
        outcome = "error"
    decisions: dict[str, Any] = {}
    for key, value in values.items():
        cleaned = _sanitize(key, value)
        if cleaned is None:
            continue
        if len(decisions) >= MAX_DECISIONS:
            logger.warning("阶段 %s 的决策字段超过 %d 个，其余被丢弃", name, MAX_DECISIONS)
            break
        decisions[cleaned[0]] = cleaned[1]
    record = StageRecord(
        stage=name,
        outcome=outcome,
        duration_seconds=round(elapsed, 4),
        variant=_resolve_variant(variant),
        decisions=decisions,
    )
    _store(run_id, record)
    STAGE_CALLS.labels(record.stage, record.outcome, record.variant).inc()
    STAGE_DURATION.labels(record.stage, record.variant).observe(elapsed)


def _store(run_id: str, record: StageRecord) -> None:
    bucket = _RUNS.get(run_id)
    if bucket is None:
        bucket = deque(maxlen=MAX_RECORDS_PER_RUN)
        _RUNS[run_id] = bucket
        while len(_RUNS) > MAX_RUNS:
            _RUNS.popitem(last=False)
    bucket.append(record)


def stages_for(run_id: str) -> list[dict[str, Any]]:
    """返回一次运行的阶段记录；没有记录时返回空列表而不是报错。"""
    bucket = _RUNS.get(str(run_id)[:MAX_RUN_ID_CHARS])
    return [record.to_dict() for record in bucket] if bucket else []


def clear_stages() -> None:
    """测试与本地调试用：清空内存中的记录，不影响已导出的指标。"""
    _RUNS.clear()
