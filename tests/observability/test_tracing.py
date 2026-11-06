import json
import runpy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool
from langfuse import Langfuse
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr, ValidationError

from feishu_tasks import MessageTaskStore
from observability import tracing
from observability.identifiers import task_id_for, trace_id_for
from schema import UserInput


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setattr(tracing, "_client", None)
    monkeypatch.setattr(tracing, "_attempted", False)
    return SimpleNamespace(
        LANGFUSE_TRACING=True,
        LANGFUSE_PUBLIC_KEY=SecretStr(f"pk-test-{uuid4()}"),
        LANGFUSE_SECRET_KEY=SecretStr("sk-private-test"),
        LANGFUSE_HOST="https://example.invalid",
        LANGFUSE_TIMEOUT=3,
        LANGFUSE_CAPTURE_CONTENT=False,
    )


def test_disabled_and_missing_credentials_never_create_sdk(config, monkeypatch):
    constructor = Mock()
    monkeypatch.setattr(tracing, "Langfuse", constructor)
    config.LANGFUSE_TRACING = False
    assert tracing.tracing_callback(config, "a" * 32) is None
    config.LANGFUSE_TRACING = True
    config.LANGFUSE_SECRET_KEY = None
    assert tracing.tracing_callback(config, "a" * 32) is None
    constructor.assert_not_called()


def test_initialization_failure_is_isolated_and_not_retried_per_request(
    config, monkeypatch, caplog
):
    constructor = Mock(side_effect=RuntimeError("sk-sensitive-body"))
    monkeypatch.setattr(tracing, "Langfuse", constructor)
    assert tracing.tracing_callback(config, "a" * 32) is None
    assert tracing.tracing_callback(config, "a" * 32) is None
    constructor.assert_called_once()
    assert "sk-sensitive-body" not in caplog.text


def test_config_is_explicit_and_callback_creation_failure_is_isolated(config, monkeypatch):
    constructor, callback = Mock(), Mock(side_effect=RuntimeError())
    monkeypatch.setattr(tracing, "Langfuse", constructor)
    monkeypatch.setattr(tracing, "SafeCallbackHandler", callback)
    assert tracing.tracing_callback(config, "a" * 32) is None
    args = constructor.call_args.kwargs
    assert args["host"] == config.LANGFUSE_HOST
    assert args["timeout"] == 3
    assert args["flush_at"] == 32
    assert args["mask"](data="private-content") == "[CONTENT OMITTED]"
    callback.assert_called_once_with(
        public_key=config.LANGFUSE_PUBLIC_KEY.get_secret_value(),
        trace_context={"trace_id": "a" * 32},
    )


def test_task_trace_survives_reopen_and_matches_backend(tmp_path):
    store = MessageTaskStore(tmp_path / "tasks.sqlite3", "app")
    task = store.enqueue("message", "FeishuMessage", {}, 32)
    store.record_invocation("message", "thread", "user")
    assert task.task_id == task_id_for("app", "message")
    reopened = MessageTaskStore(store.path, "app").get(task.task_id)
    assert reopened.context["trace_id"] == trace_id_for(task.task_id, uuid4())
    assert trace_id_for(None, uuid4()) != reopened.context["trace_id"]


def test_masking_default_and_opt_in():
    assert tracing.mask_content(data={"messages": "private", "task_id": "id"}) == {"task_id": "id"}
    masked = tracing.mask_content(
        data={"password": "private", "text": "Bearer abcdef and api_key=hello and sk-private-key"},
        capture=True,
    )
    assert masked["password"] == "[REDACTED]"
    for value in ("abcdef", "hello", "sk-private-key"):
        assert value not in masked["text"]


def test_task_id_validation():
    assert UserInput(message="test", task_id="a" * 64).task_id
    with pytest.raises(ValidationError):
        UserInput(message="test", task_id="arbitrary-private-input")


def test_cli_readback_uses_observations_and_checks_trace_id(monkeypatch, capsys):
    module = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/check_langfuse.py"))
    monkeypatch.setattr(module["time"], "sleep", Mock())
    client = Mock()
    trace_id = "c" * 32
    client.api.observations.get_many.side_effect = [
        SimpleNamespace(data=[SimpleNamespace(trace_id="other")]),
        SimpleNamespace(data=[SimpleNamespace(trace_id=trace_id)]),
    ]
    assert module["verify_trace"](client, trace_id, {}) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "verified"
    assert client.api.observations.get_many.call_count == 2
    client.api.trace.get.assert_not_called()


def test_cli_readback_stops_on_rate_limit_without_printing_body(monkeypatch, capsys):
    module = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/check_langfuse.py"))
    sleep = Mock()
    monkeypatch.setattr(module["time"], "sleep", sleep)
    error = RuntimeError("private-server-body")
    error.status_code = 429
    client = Mock()
    client.api.observations.get_many.side_effect = error
    assert module["verify_trace"](client, "c" * 32, {}) == 1
    output = capsys.readouterr().out
    assert "private-server-body" not in output
    assert json.loads(output)["http_status"] == 429
    client.api.observations.get_many.assert_called_once()
    sleep.assert_not_called()


@pytest.mark.asyncio
async def test_api_correlation_and_no_network_health(config, monkeypatch):
    from service import service

    handler = Mock()
    monkeypatch.setattr(service, "tracing_callback", Mock(return_value=handler))
    agent = Mock(aget_state=AsyncMock(return_value=SimpleNamespace(tasks=[])))
    task_id = "a" * 64
    args, run_id = await service._handle_input(
        UserInput(message="test", task_id=task_id, thread_id="thread", user_id="raw-user"),
        agent,
        "dev-issue-agent",
    )
    metadata = args["config"]["metadata"]
    assert metadata["task_id"] == task_id
    assert metadata["trace_id"] == trace_id_for(task_id, run_id)
    assert metadata["langfuse_session_id"] == "thread"
    assert metadata["langfuse_user_id"] != "raw-user"
    assert args["config"]["callbacks"] == [handler]
    # Health does not authenticate against Cloud on every scrape.
    client = Mock()
    monkeypatch.setattr(tracing, "_client", client)
    result = await service.health_check()
    assert result["status"] == "ok"
    client.auth_check.assert_not_called()


def test_real_sdk_chain_model_tool_usage_and_privacy(config, monkeypatch):
    """Real callback + in-memory exporter; synthetic tokens are fixtures, never cloud traffic."""
    exporter = InMemorySpanExporter()
    constructor = Langfuse

    def create(**kwargs):
        return constructor(**kwargs, tracer_provider=TracerProvider(), span_exporter=exporter)

    monkeypatch.setattr(tracing, "Langfuse", create)
    run_id, task_id = uuid4(), "b" * 64
    trace_id = trace_id_for(task_id, run_id)
    handler = tracing.tracing_callback(config, trace_id)
    assert handler is not None
    model = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="PRIVATE-ANSWER",
                usage_metadata={"input_tokens": 11, "output_tokens": 5, "total_tokens": 16},
            )
        ]
    )

    @tool
    def synthetic_search(query: str) -> dict:
        """Return a synthetic failure without external IO."""
        return {"status": "error", "message": "PRIVATE-TOOL-OUTPUT"}

    def invoke(message, config):
        result = model.invoke(message, config)
        synthetic_search.invoke({"query": "PRIVATE-QUERY"}, config)
        return result

    try:
        RunnableLambda(invoke).invoke(
            "PRIVATE-PROMPT",
            config={
                "run_id": run_id,
                "callbacks": [handler],
                "metadata": tracing.trace_metadata(
                    "test", "session", "user", run_id, trace_id, task_id
                ),
            },
        )
        tracing.get_tracing_client(config).flush()
        spans = exporter.get_finished_spans()
        assert len(spans) >= 3
        assert {format(span.context.trace_id, "032x") for span in spans} == {trace_id}
        attributes = json.dumps([dict(span.attributes) for span in spans])
        for value in ("PRIVATE-ANSWER", "PRIVATE-TOOL-OUTPUT", "PRIVATE-QUERY", "PRIVATE-PROMPT"):
            assert value not in attributes
        assert task_id in attributes
        assert any(span.attributes.get("session.id") == "session" for span in spans)
        assert all(span.attributes.get("user.id") != "user" for span in spans)
        assert any(
            "usage" in key and "11" in str(value)
            for span in spans
            for key, value in span.attributes.items()
        )
        assert any(span.attributes.get("langfuse.observation.level") == "ERROR" for span in spans)
    finally:
        tracing.shutdown_tracing()
