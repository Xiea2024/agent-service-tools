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
