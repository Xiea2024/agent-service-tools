"""研发问题处理 Agent 的离线测试，覆盖图执行、API 接入和会话上下文传递。

使用真实 LangGraph 与内存 checkpoint，模型调用用 AsyncMock 替代以避免外部请求。
测试验证工程链路，不代表真实模型已正确完成故障分类与按需追问。
"""

import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver

from agents.agents import Agent, agents, get_agent, get_all_agent_info
from agents.dev_issue_agent import agent, dev_issue_agent, respond
from core import settings
from schema.models import FakeModelName
from service import app


@pytest.fixture
def model(monkeypatch):
    """替换实际 Provider，并关闭会触发外部请求的可选追踪配置。"""
    model = Mock()
    model.bind_tools.return_value = model
    model.ainvoke = AsyncMock(return_value=AIMessage(content="请补充失败时间和响应码。"))
    factory = Mock(return_value=model)
    monkeypatch.setattr("agents.dev_issue_agent.get_model", factory)
    monkeypatch.setattr(settings, "AUTH_SECRET", None)
    monkeypatch.setattr(settings, "LANGFUSE_TRACING", False)
    return model, factory


def test_registered_without_replacing_existing_agents():
    assert get_agent("dev-issue-agent") is dev_issue_agent
    assert {"chatbot", "research-assistant", "dev-issue-agent"} <= {
        info.key for info in get_all_agent_info()
    }


@pytest.mark.asyncio
async def test_selected_and_default_model_are_forwarded(model):
    mock_model, factory = model
    state = {"messages": [HumanMessage(content="订单接口返回 500")]}
    config = {"configurable": {"model": FakeModelName.FAKE}}
    await respond(state, config)
    factory.assert_called_with(FakeModelName.FAKE)
    assert mock_model.ainvoke.await_args.args[1] is config
    await respond(state, {})
    factory.assert_called_with(settings.DEFAULT_MODEL)


@pytest.mark.asyncio
async def test_invoke_preserves_history_and_isolates_threads(model, monkeypatch):
    """通过真实 API 和注册表调用图，确认追问后补充的信息能进入下一轮模型输入。"""
    mock_model, _ = model
    # 每轮两次模型调用：supervisor 委派（这里没有 tool_calls，按规则回退到文档专家）+ 专家回答。
    delegated = AIMessage(content="委派给文档专家。")
    mock_model.ainvoke.side_effect = [
        delegated,
        AIMessage(content="请补充失败时间和响应码。"),
        delegated,
        AIMessage(content="已记录今天十点和 500，根因仍待验证。"),
        delegated,
        AIMessage(content="请提供启动命令和缺失依赖的名称。"),
    ]
    graph = agent.compile(checkpointer=MemorySaver())
    monkeypatch.setitem(agents, "dev-issue-agent", Agent(description="测试", graph_like=graph))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for message, thread in [
            ("订单接口返回 500", "api-error"),
            ("今天十点，响应码 500", "api-error"),
            ("启动时提示依赖缺失", "startup"),
        ]:
            response = await client.post(
                "/dev-issue-agent/invoke", json={"message": message, "thread_id": thread}
            )
            assert response.status_code == 200
            assert response.json()["type"] == "ai"
        metadata = await client.get("/info")
        assert "dev-issue-agent" in {item["key"] for item in metadata.json()["agents"]}
    calls = mock_model.ainvoke.await_args_list
    # 偶数下标是 supervisor 的委派调用，奇数下标才是文档专家的回答调用：
    # 第 2 轮专家调用在 calls[3]，第 3 轮在 calls[5]。
    assert isinstance(calls[3].args[0][0], SystemMessage)
    assert [message.content for message in calls[3].args[0][1:]] == [
        "订单接口返回 500",
        "请补充失败时间和响应码。",
        "今天十点，响应码 500",
    ]
    assert [message.content for message in calls[5].args[0][1:]] == ["启动时提示依赖缺失"]
    saved = await graph.aget_state({"configurable": {"thread_id": "api-error"}})
    assert [message.type for message in saved.values["messages"]] == ["human", "ai", "human", "ai"]


@pytest.mark.asyncio
async def test_stream_returns_a_message_event(model, monkeypatch):
    graph = agent.compile(checkpointer=MemorySaver())
    monkeypatch.setitem(agents, "dev-issue-agent", Agent(description="测试", graph_like=graph))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/dev-issue-agent/stream",
            json={"message": "升级后单元测试失败", "thread_id": "stream", "stream_tokens": False},
        )
    assert response.status_code == 200
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    messages = [event["content"] for event in events if event["type"] == "message"]
    assert messages[-1]["content"] == "请补充失败时间和响应码。"


@pytest.mark.asyncio
async def test_provider_error_is_not_replaced_with_a_success_message(model):
    mock_model, _ = model
    mock_model.ainvoke.side_effect = RuntimeError("provider unavailable")
    with pytest.raises(RuntimeError, match="provider unavailable"):
        await agent.compile().ainvoke({"messages": [HumanMessage(content="订单接口返回 500")]})
