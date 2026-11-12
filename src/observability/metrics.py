"""Low-cardinality process metrics shared by the API and the Feishu exporter."""

from contextlib import contextmanager
from time import perf_counter

from prometheus_client import Counter, Histogram

CALLS = Counter(
    "supportpilot_operations_total",
    "Completed operations (not business answer quality).",
    ["operation", "outcome"],
)
DURATION = Histogram(
    "supportpilot_operation_duration_seconds",
    "Operation wall time including SDK retries.",
    ["operation"],
    buckets=(0.1, 0.5, 1, 2, 5, 10, 20, 40, 65, 90),
)

# 阶段指标：固定 stage 名 + 固定 outcome + 受控 variant，标签基数完全由代码枚举决定。
# 与 supportpilot_operations_total 的关系是“同一事实的两种切法”：operation 描述一次
# 外部调用，stage 描述一轮问答里的一步，用于判断哪一步开始出错。
STAGE_CALLS = Counter(
    "supportpilot_agent_stage_total",
    "Completed agent stages by outcome, one series per fixed stage name.",
    ["stage", "outcome", "variant"],
)
STAGE_DURATION = Histogram(
    "supportpilot_agent_stage_duration_seconds",
    "Agent stage wall time, including the work that stage wraps.",
    ["stage", "variant"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 25, 60),
)


@contextmanager
def operation(name: str):
    """Call sites supply fixed names/outcomes, never exception messages or identifiers."""
    started = perf_counter()
    result = {"outcome": "success"}
    try:
        yield result
    except TimeoutError:
        result["outcome"] = "timeout"
        raise
    except BaseException:
        result["outcome"] = "error"
        raise
    finally:
        CALLS.labels(name, result["outcome"]).inc()
        DURATION.labels(name).observe(perf_counter() - started)
