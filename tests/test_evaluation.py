import asyncio
import json
import runpy
from pathlib import Path

import pytest

from evaluation import Case, Suite, Turn, check_turn, load_suite, run_evaluation
from schema import ChatMessage

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "https://example.com/cors"


def ai(content="回答", calls=None, run_id="run-1"):
    return ChatMessage(type="ai", content=content, tool_calls=calls or [], run_id=run_id)


def retrieval_messages(status="ok"):
    call = {"name": "search_feishu_documents", "args": {"query": "跨域"}, "id": "call-1"}
    hits = (
        []
        if status != "ok"
        else [
            {
                "chunk_id": "chunk-1",
                "document_id": "doc-1",
                "title": "跨域文档",
                "source_url": SOURCE,
                "content": "合成资料",
                "score": 0.8,
                "truncated": False,
            }
        ]
    )
    return [
        ai("", [call]),
        ChatMessage(
            type="tool",
            content=json.dumps({"status": status, "hits": hits}),
            tool_call_id="call-1",
            run_id="run-1",
        ),
        ai(f"参考[跨域文档]({SOURCE})。" if hits else "当前未找到资料。"),
    ]


class FakeClient:
    agent = "dev-issue-agent"
    base_url = "http://offline.test"

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def astream(self, message, **kwargs):
        self.requests.append({"message": message, **kwargs})
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        for item in response:
            yield item


def suite(*cases):
    return Suite(id="test", version="1", description="离线测试", cases=list(cases))


def case(case_id="one", turns=1):
    return Case(
        id=case_id,
        title="合成案例",
        category="test",
        turns=[Turn(message=f"问题 {index}", rubric=["人工检查"]) for index in range(turns)],
    )


def test_fixed_suite_is_valid_and_selection_preserves_order():
    fixed = load_suite(ROOT / "evals/supportpilot/cases.json")
    assert len(fixed.cases) == 10
    assert sum(len(c.turns) for c in fixed.cases) == 12
    selected = load_suite(ROOT / "evals/supportpilot/cases.json", ["SP10", "SP01"])
    assert [c.id for c in selected.cases] == ["SP01", "SP10"]
    with pytest.raises(ValueError, match="未知案例"):
        load_suite(ROOT / "evals/supportpilot/cases.json", ["missing"])
    with pytest.raises(ValueError, match="不能重复"):
        suite(case(), case())
    with pytest.raises(ValueError, match="空白"):
        Turn(message=" \n", rubric=["人工检查"])


def test_evidence_matches_calls_and_real_sources():
    turn = Turn(
        message="请查文档",
        retrieval="required",
        rubric=["检查适用性"],
        expected_document_ids=["doc-1"],
        expected_status="ok",
    )
    checks, evidence, answer = check_turn(turn, retrieval_messages(), set())
    assert all(c["passed"] for c in checks)
    assert evidence[0]["hits"][0]["content"] == "合成资料"
    assert SOURCE in answer


@pytest.mark.parametrize(
    "status,expected,passes",
    [
        ("no_results", None, True),
        ("error", None, False),
        ("error", "error", True),
    ],
)
def test_no_results_and_service_failure_are_distinct(status, expected, passes):
    turn = Turn(message="查文档", rubric=["检查"], expected_status=expected)
    checks, evidence, _ = check_turn(turn, retrieval_messages(status), set())
    assert all(c["passed"] for c in checks) is passes
    assert evidence[0]["status"] == status


def test_invalid_tool_results_and_unmatched_ids_are_retained_as_failures():
    messages = retrieval_messages()
    messages[1].tool_call_id = None
    messages[1].content = "检索工具执行失败：RuntimeError，请检查后台。"
    checks, evidence, _ = check_turn(Turn(message="查询", rubric=["检查"]), messages, set())
    assert not next(c for c in checks if c["name"] == "tool_calls_matched")["passed"]
    assert not next(c for c in checks if c["name"] == "tool_payload")["passed"]
    assert evidence == []


def test_unsupported_url_and_agent_fallback_fail_rules():
    turn = Turn(message="查询", rubric=["检查"])
    checks, _, _ = check_turn(turn, [ai("[伪造来源](https://invented.test/doc)")], set())
    assert not next(c for c in checks if c["name"] == "source_urls")["passed"]
    checks, _, _ = check_turn(turn, [ai("本轮模型响应超时，请稍后重试。")], set())
    assert not next(c for c in checks if c["name"] == "agent_completed")["passed"]


@pytest.mark.asyncio
async def test_multiturn_isolation_snapshots_and_report(tmp_path):
    client = FakeClient([retrieval_messages(), [ai(f"仍参考 {SOURCE}")], [ai()]])
    output = tmp_path / "run"
    result = await run_evaluation(suite(case(turns=2), case("two")), client, output)
    assert result["passed_cases"] == 2
    assert result["quality_review_pending"] == 3
    assert client.requests[0]["thread_id"] == client.requests[1]["thread_id"]
    assert client.requests[0]["thread_id"] != client.requests[2]["thread_id"]
    assert all(request["stream_tokens"] is False for request in client.requests)
    records = [
        json.loads(line) for line in (output / "results.jsonl").read_text("utf-8").splitlines()
    ]
    assert records[0]["messages"][1]["type"] == "tool"
    assert records[0]["quality_review"]["status"] == "pending"
    assert records[0]["run_id"] == "run-1"
    assert (output / "cases.json").is_file()
    assert json.loads((output / "manifest.json").read_text("utf-8"))["status"] == "completed"
    report = (output / "report.md").read_text("utf-8")
    assert "2/2" in report and "不代表答案正确率" in report and "合成资料" in report
    with pytest.raises(FileExistsError):
        await run_evaluation(suite(case()), client, output)


@pytest.mark.asyncio
async def test_partial_failure_is_saved_and_other_cases_continue(tmp_path, capsys):
    client = FakeClient([RuntimeError("连接失败"), [ai()]])
    output = tmp_path / "run"
    result = await run_evaluation(suite(case(turns=2), case("two")), client, output)
    assert result["total_turns"] == 3
    assert result["recorded_turns"] == 2
    assert result["unrecorded_turns"] == 1
    assert result["turn_counts"] == {"passed": 1, "failed": 0, "error": 1}
    assert result["case_pass_rate"] == 0.5
    records = [
        json.loads(line) for line in (output / "results.jsonl").read_text("utf-8").splitlines()
    ]
    assert records[0]["error"]["message"] == "连接失败"
    assert "RuntimeError: 连接失败" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_sse_error_is_not_counted_as_success(tmp_path):
    client = FakeClient([[ai("Error: Internal server error", run_id=None)]])
    result = await run_evaluation(suite(case()), client, tmp_path / "run")
    assert result["turn_counts"]["error"] == 1


@pytest.mark.asyncio
async def test_cancellation_keeps_existing_results_and_marks_interrupted(tmp_path):
    class CancelClient(FakeClient):
        async def astream(self, message, **kwargs):
            if self.requests:
                raise asyncio.CancelledError
            async for item in super().astream(message, **kwargs):
                yield item

    output = tmp_path / "run"
    with pytest.raises(asyncio.CancelledError):
        await run_evaluation(suite(case(turns=2)), CancelClient([[ai()]]), output)
    manifest = json.loads((output / "manifest.json").read_text("utf-8"))
    assert manifest["status"] == "interrupted"
    assert len((output / "results.jsonl").read_text("utf-8").splitlines()) == 1
    assert "interrupted" in (output / "report.md").read_text("utf-8")


def test_cli_validation_does_not_create_client(monkeypatch, capsys):
    def unexpected_client(*args, **kwargs):
        raise AssertionError("校验案例不应访问后端")

    monkeypatch.setattr("client.AgentClient", unexpected_client)
    monkeypatch.setattr("sys.argv", ["evaluate_supportpilot.py", "--validate-only"])
    runpy.run_path(str(ROOT / "scripts/evaluate_supportpilot.py"), run_name="__main__")
    assert "10 个案例，12 轮" in capsys.readouterr().out


@pytest.mark.parametrize(
    "reason",
    [
        "Error getting service info: All connection attempts failed",
        "Error getting service info: 401 Unauthorized",
        "Agent dev-issue-agent not found in available agents: chatbot",
    ],
)
def test_cli_preflight_failure_does_not_run_cases(tmp_path, monkeypatch, capsys, reason):
    from unittest.mock import AsyncMock, Mock

    from client import AgentClientError

    client_factory = Mock(side_effect=AgentClientError(reason))
    runner = AsyncMock()
    output = tmp_path / "run"
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args: None)
    monkeypatch.setattr("client.AgentClient", client_factory)
    monkeypatch.setattr("evaluation.run_evaluation", runner)
    monkeypatch.setattr(
        "sys.argv",
        [
            "evaluate_supportpilot.py",
            "--output-dir",
            str(output),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(ROOT / "scripts/evaluate_supportpilot.py"), run_name="__main__")
    assert exc.value.code == 2
    message = capsys.readouterr().err
    assert "评测未开始" in message and reason in message
    assert "src/run_service.py" in message
    runner.assert_not_called()
    assert not output.exists()


def test_cli_checks_model_before_running_cases(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    client_factory = Mock(return_value=SimpleNamespace(info=SimpleNamespace(models=["fake"])))
    runner = AsyncMock()
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args: None)
    monkeypatch.setattr("client.AgentClient", client_factory)
    monkeypatch.setattr("evaluation.run_evaluation", runner)
    monkeypatch.setattr(
        "sys.argv",
        [
            "evaluate_supportpilot.py",
            "--model",
            "openai-compatible",
            "--output-dir",
            str(tmp_path / "run"),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(ROOT / "scripts/evaluate_supportpilot.py"), run_name="__main__")
    assert exc.value.code == 2
    assert "服务未提供模型" in capsys.readouterr().err
    runner.assert_not_called()


def test_cli_preflight_uses_short_timeout_then_runs(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    client = SimpleNamespace(info=SimpleNamespace(models=["fake"]), timeout=5)
    client_factory = Mock(return_value=client)
    runner = AsyncMock(return_value={"passed_cases": 1, "total_cases": 1})
    output = tmp_path / "run"
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args: None)
    monkeypatch.setattr("client.AgentClient", client_factory)
    monkeypatch.setattr("evaluation.run_evaluation", runner)
    monkeypatch.setattr(
        "sys.argv",
        [
            "evaluate_supportpilot.py",
            "--case",
            "SP01",
            "--timeout",
            "75",
            "--base-url",
            "http://localhost:8080/",
            "--output-dir",
            str(output),
        ],
    )
    runpy.run_path(str(ROOT / "scripts/evaluate_supportpilot.py"), run_name="__main__")
    client_factory.assert_called_once_with(
        base_url="http://localhost:8080",
        agent="dev-issue-agent",
        timeout=5,
    )
    assert client.timeout == 75
    runner.assert_awaited_once()
    assert runner.await_args.args[2] == output


@pytest.mark.asyncio
async def test_turn_timeout_preserves_failure(tmp_path):
    class SlowClient(FakeClient):
        async def astream(self, message, **kwargs):
            await asyncio.sleep(1)
            yield ai()

    output = tmp_path / "run"
    result = await run_evaluation(suite(case()), SlowClient([]), output, timeout=0.01)
    assert result["turn_counts"]["error"] == 1
    record = json.loads((output / "results.jsonl").read_text("utf-8"))
    assert record["error"]["type"] == "TimeoutError"


@pytest.mark.asyncio
async def test_existing_client_sse_api_and_real_graph(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, Mock

    import httpx
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import MemorySaver

    import document_retrieval
    from agents.dev_issue_agent import agent
    from client import AgentClient
    from core import settings
    from document_retrieval import SearchHit
    from service import app

    model = Mock()
    model.bind_tools.return_value = model
    model.ainvoke = AsyncMock(
        side_effect=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_feishu_documents",
                        "args": {"query": "跨域"},
                        "id": "call-1",
                    }
                ],
            ),
            AIMessage(content=f"参考[跨域文档]({SOURCE})。"),
        ]
    )
    search = Mock(
        return_value=[
            SearchHit(
                chunk_id="chunk-1",
                document_id="doc-1",
                title="跨域文档",
                source_url=SOURCE,
                content="合成资料",
                score=0.8,
            )
        ]
    )
    monkeypatch.setattr("agents.dev_issue_agent.get_model", lambda _: model)
    monkeypatch.setattr(document_retrieval, "search_documents", search)
    monkeypatch.setattr(settings, "AUTH_SECRET", None)
    monkeypatch.setattr(settings, "LANGFUSE_TRACING", False)
    graph = agent.compile(checkpointer=MemorySaver())
    monkeypatch.setattr("service.service.get_agent", lambda _: graph)
    real_http_client = httpx.AsyncClient
    monkeypatch.setattr(
        "client.client.httpx.AsyncClient",
        lambda: real_http_client(transport=httpx.ASGITransport(app=app)),
    )
    client = AgentClient(base_url="http://test", get_info=False)
    client.update_agent("dev-issue-agent", verify=False)
    test_case = Case(
        id="rag",
        title="真实 SSE 链路",
        category="rag",
        turns=[Turn(message="请查跨域文档", retrieval="required", rubric=["检查适用性"])],
    )
    output = tmp_path / "run"
    result = await run_evaluation(suite(test_case), client, output)
    assert result["passed_cases"] == 1
    record = json.loads((output / "results.jsonl").read_text("utf-8"))
    assert [message["type"] for message in record["messages"]] == ["ai", "tool", "ai"]
    assert record["evidence"][0]["hits"][0]["document_id"] == "doc-1"
    assert record["run_id"]
    search.assert_called_once_with("跨域")
