"""阶段记录：指标标签、内容隔离、异常语义与有界存储。

这些测试只验证“记录本身是对的”，不使用真实模型或 Qdrant。
"""

import pytest

from observability import stages
from observability.metrics import STAGE_CALLS

CONFIG = {"run_id": "run-1"}


@pytest.fixture(autouse=True)
def clean():
    stages.clear_stages()
    yield
    stages.clear_stages()


def counter(stage: str, outcome: str, variant: str) -> float:
    return STAGE_CALLS.labels(stage, outcome, variant)._value.get()


def test_stage_records_outcome_decisions_and_metric():
    before = counter(stages.RETRIEVE, "success", "v1")
    with stages.stage(stages.RETRIEVE, config=CONFIG, variant="v1") as observed:
        observed["hits"] = 2
        observed["top_score"] = 0.712345
    records = stages.stages_for("run-1")
    assert len(records) == 1
    assert records[0]["stage"] == "retrieve"
    assert records[0]["outcome"] == "success"
    assert records[0]["variant"] == "v1"
    assert records[0]["decisions"] == {"hits": 2, "top_score": 0.7123}
    assert records[0]["duration_seconds"] >= 0
    assert counter(stages.RETRIEVE, "success", "v1") == before + 1


def test_call_site_can_override_outcome():
    with stages.stage(stages.RETRIEVE, config=CONFIG, variant="v1") as observed:
        observed["hits"] = 0
        observed["outcome"] = "no_results"
    assert stages.stages_for("run-1")[0]["outcome"] == "no_results"
    assert "outcome" not in stages.stages_for("run-1")[0]["decisions"]


def test_timeout_and_error_are_recorded_and_reraised():
    with pytest.raises(TimeoutError):
        with stages.stage(stages.GENERATE, config=CONFIG, variant="v1"):
            raise TimeoutError("private-timeout-detail")
    with pytest.raises(RuntimeError):
        with stages.stage(stages.GENERATE, config=CONFIG, variant="v1"):
            raise RuntimeError("private-error-detail")
    outcomes = [record["outcome"] for record in stages.stages_for("run-1")]
    assert outcomes == ["timeout", "error"]


def test_unknown_stage_is_rejected_instead_of_creating_a_new_series():
    with pytest.raises(ValueError, match="unknown stage"):
        with stages.stage("retrival", config=CONFIG):
            pass


def test_decisions_drop_content_and_keep_low_cardinality():
    long_text = "用户原文" * 200
    with stages.stage(stages.VERIFY, config=CONFIG, variant="v1") as observed:
        observed["answer_chars"] = 42
        observed["note"] = long_text
        observed["nested"] = {"secret": "value"}
        observed["Bad-Key"] = 1
        observed["nan"] = float("nan")
    decisions = stages.stages_for("run-1")[0]["decisions"]
    assert decisions["answer_chars"] == 42
    assert len(decisions["note"]) <= stages.MAX_STR_CHARS
    assert set(decisions) == {"answer_chars", "note"}


def test_decision_count_is_bounded():
    with stages.stage(stages.ROUTE, config=CONFIG, variant="v1") as observed:
        for index in range(stages.MAX_DECISIONS + 4):
            observed[f"key_{index}"] = index
    assert len(stages.stages_for("run-1")[0]["decisions"]) == stages.MAX_DECISIONS


def test_records_are_bounded_per_run_and_in_total():
    for _ in range(stages.MAX_RECORDS_PER_RUN + 3):
        with stages.stage(stages.GENERATE, config=CONFIG, variant="v1"):
            pass
    assert len(stages.stages_for("run-1")) == stages.MAX_RECORDS_PER_RUN

    for index in range(stages.MAX_RUNS + 5):
        with stages.stage(stages.GENERATE, config={"run_id": f"run-{index}"}, variant="v1"):
            pass
    assert stages.stages_for("run-0") == []
    assert len(stages.stages_for(f"run-{stages.MAX_RUNS + 4}")) == 1


def test_runs_without_identifier_share_one_bucket():
    with stages.stage(stages.EVIDENCE) as observed:
        observed["hits"] = 1
    with stages.stage(stages.EVIDENCE) as observed:
        observed["hits"] = 1
    assert len(stages.stages_for("_unbound")) == 2


def test_variant_falls_back_when_unsupported():
    with stages.stage(stages.GENERATE, config=CONFIG, variant="Bad Variant!"):
        pass
    assert stages.stages_for("run-1")[0]["variant"] == "v1"


def test_run_id_is_read_from_configurable_too():
    with stages.stage(stages.GENERATE, config={"configurable": {"run_id": "nested-run"}}):
        pass
    assert len(stages.stages_for("nested-run")) == 1
