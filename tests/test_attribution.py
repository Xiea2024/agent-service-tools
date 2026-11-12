"""失败归因规则：每个阶段各一条用例，外加“无法归因”与“全部通过”。"""

from evaluation import Turn
from harness.attribution import STAGE_ORDER, attribute, summarize

SOURCE = "https://example.feishu.cn/docx/doc-1"


def turn(**kwargs) -> Turn:
    base = {"message": "接口返回 500", "rubric": ["人工检查"]}
    base.update(kwargs)
    return Turn(**base)


def result(
    checks: dict[str, bool] | None = None,
    hits: int = 0,
    cited: int = 0,
    calls: int = 0,
    answer: str = "请补充失败时间。",
    stages: list[dict] | None = None,
    status: str | None = None,
    error: dict | None = None,
) -> dict:
    check_list = [
        {"name": name, "passed": passed, "detail": name} for name, passed in (checks or {}).items()
    ]
    evidence = []
    if hits:
        evidence.append(
            {
                "tool_call_id": "call-1",
                "status": "ok",
                "hits": [
                    {
                        "chunk_id": f"chunk-{index}",
                        "document_id": f"doc-{index}",
                        "title": "跨域文档",
                        "source_url": SOURCE,
                        "content": "正文",
                        "score": 0.8,
                    }
                    for index in range(hits)
                ],
            }
        )
    return {
        "status": status
        or ("passed" if all(value for _, value in (checks or {}).items()) else "failed"),
        "answer": answer,
        "messages": [
            {"type": "ai", "tool_calls": [{"id": f"call-{index}"} for index in range(calls)]},
        ],
        "checks": check_list,
        "evidence": evidence,
        "stages": stages
        or (
            [
                {
                    "stage": "evidence",
                    "outcome": "success",
                    "variant": "v1",
                    "decisions": {"hits": hits, "cited": cited},
                }
            ]
            if hits
            else []
        ),
        "error": error,
    }


def test_passed_turn_has_no_attribution():
    assert attribute(turn(), result({"answer_present": True}))["stage"] == "none"


def test_required_retrieval_without_tool_call_is_a_route_failure():
    verdict = attribute(
        turn(retrieval="required"), result({"retrieval_policy": False, "answer_present": True})
    )
    assert verdict["stage"] == "route"
    assert "没有调用工具" in verdict["reason"]


def test_forbidden_retrieval_with_tool_call_is_a_route_failure():
    verdict = attribute(
        turn(retrieval="forbidden"),
        result({"retrieval_policy": False, "answer_present": True}, calls=1),
    )
    assert verdict["stage"] == "route"
    assert "不检索" in verdict["reason"]


def test_retrieval_error_is_a_retrieve_failure():
    stages = [{"stage": "retrieve", "outcome": "error", "decisions": {"error_code": "qdrant"}}]
    verdict = attribute(
        turn(retrieval="required"),
        result({"retrieval_available": False, "answer_present": True}, calls=1, stages=stages),
    )
    assert verdict["stage"] == "retrieve"
    assert "qdrant" in verdict["reason"]


def test_missing_expected_documents_is_a_retrieve_failure():
    verdict = attribute(
        turn(retrieval="required", expected_document_ids=["doc-9"]),
        result({"expected_documents": False, "answer_present": True}, hits=1, calls=1),
    )
    assert verdict["stage"] == "retrieve"


def test_hits_without_citation_is_an_evidence_failure():
    verdict = attribute(
        turn(retrieval="required"),
        result({"evidence_cited": False, "answer_present": True}, hits=2, cited=0, calls=1),
    )
    assert verdict["stage"] == "evidence"
    assert "没有被回答引用" in verdict["reason"]


def test_unsupported_link_is_an_evidence_failure():
    verdict = attribute(
        turn(retrieval="required"),
        result(
            {"source_urls": False}, hits=1, cited=1, calls=1, answer="见 https://fake.example/x"
        ),
    )
    assert verdict["stage"] == "evidence"
    assert "没有证据支持的链接" in verdict["reason"]


def test_cited_evidence_passes():
    verdict = attribute(
        turn(retrieval="required"),
        result(
            {"answer_present": True, "evidence_cited": True},
            hits=1,
            cited=1,
            calls=1,
            answer=f"参考[文档]({SOURCE})",
        ),
    )
    assert verdict["stage"] == "none"


def test_empty_answer_is_a_generate_failure():
    verdict = attribute(turn(), result({"answer_present": False}, answer=""))
    assert verdict["stage"] == "generate"


def test_model_budget_exhaustion_is_a_generate_failure():
    stages = [{"stage": "generate", "outcome": "budget_exhausted", "decisions": {}}]
    verdict = attribute(turn(), result({"agent_completed": False}, stages=stages))
    assert verdict["stage"] == "generate"
    assert "预算耗尽" in verdict["reason"]


def test_protocol_mismatch_is_a_verify_failure():
    verdict = attribute(turn(), result({"run_id_consistent": False}, calls=1))
    assert verdict["stage"] == "verify"


def test_unknown_failure_is_not_forced_into_a_stage():
    verdict = attribute(turn(), result({"some_new_check": False}))
    assert verdict["stage"] == "unattributed"
    assert verdict["failed_checks"] == ["some_new_check"]


def test_pipeline_order_prefers_the_earliest_stage():
    """检索失败时，后面的“没引用”是结果而不是原因。"""
    stages = [{"stage": "retrieve", "outcome": "timeout", "decisions": {}}]
    verdict = attribute(
        turn(retrieval="required"),
        result(
            {"source_urls": False, "answer_present": True}, hits=1, cited=0, calls=1, stages=stages
        ),
    )
    assert verdict["stage"] == "retrieve"


def test_variant_is_reported_for_ab_comparison():
    stages = [{"stage": "generate", "outcome": "timeout", "variant": "v2-rerank", "decisions": {}}]
    verdict = attribute(turn(), result({"answer_present": False}, stages=stages))
    assert verdict["variant"] == "v2-rerank"


def test_summary_counts_only_failed_stages():
    passed = {"attribution": {"stage": "none"}}
    retrieve = {"attribution": {"stage": "retrieve"}}
    retrieve_again = {"attribution": {"stage": "retrieve"}}
    unknown = {"attribution": {"stage": "unattributed"}}
    summary = summarize([passed, retrieve, retrieve_again, unknown])
    assert summary["stage_failures"] == {"retrieve": 2, "unattributed": 1}
    assert summary["unattributed_turns"] == 1
    assert set(summary["stage_failures"]) <= set(STAGE_ORDER) | {"unattributed"}
