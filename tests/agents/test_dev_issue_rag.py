"""研发 Agent 工具循环测试：真实 LangGraph/ToolNode，假模型和检索结果。

覆盖 ToolMessage 与出处回传、检索失败、循环预算和新请求预算重置。
模型由 AsyncMock 替代，不评估真实模型的回答质量。
"""

import json
from time import monotonic
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver

import document_retrieval
from agents.agents import Agent, agents
from agents.dev_issue_agent import agent, respond, run_tools
from core import settings
from document_retrieval import RetrievalError, SearchHit
from service import app


def tool_call(index=1):
    """模拟模型选择工具；这条 AIMessage 自身并没有执行检索。"""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "search_feishu_documents",
                "args": {"query": "跨域问题怎么排查"},
                "id": f"call-{index}",
                "type": "tool_call",
            }
        ],
    )


@pytest.fixture
def rag_model(monkeypatch):
    model = Mock()
    model.bind_tools.return_value = model
    model.ainvoke = AsyncMock()
    monkeypatch.setattr("agents.dev_issue_agent.get_model", lambda _: model)
    monkeypatch.setattr(settings, "AUTH_SECRET", None)
    monkeypatch.setattr(settings, "LANGFUSE_TRACING", False)
    return model


@pytest.mark.asyncio
async def test_api_tool_result_reaches_model_and_final_answer(rag_model, monkeypatch):
    hit = SearchHit(
        chunk_id="point",
        document_id="doc",
        title="跨域说明",
        source_url="https://example.com/doc",
        content="检查允许的 Origin。",
        score=0.8,
    )
    search = Mock(return_value=[hit])
    monkeypatch.setattr(document_retrieval, "search_documents", search)
    rag_model.ainvoke.side_effect = [
        tool_call(),
        AIMessage(content="参考[跨域说明](https://example.com/doc)。"),
    ]
    graph = agent.compile(checkpointer=MemorySaver())
    monkeypatch.setitem(agents, "dev-issue-agent", Agent(description="测试", graph_like=graph))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/dev-issue-agent/invoke", json={"message": "请查跨域文档", "thread_id": "rag"}
        )
    assert response.status_code == 200
    assert "https://example.com/doc" in response.json()["content"]
    search.assert_called_once_with("跨域问题怎么排查")
    second_input = rag_model.ainvoke.await_args_list[1].args[0]
    tool_message = next(message for message in second_input if isinstance(message, ToolMessage))
    assert tool_message.tool_call_id == "call-1"
    assert json.loads(tool_message.content)["hits"][0]["content"] == hit.content
    saved = await graph.aget_state({"configurable": {"thread_id": "rag"}})
    assert [m.type for m in saved.values["messages"]] == ["human", "ai", "tool", "ai"]


@pytest.mark.asyncio
async def test_retrieval_failure_is_given_to_model_as_error(rag_model, monkeypatch):
    monkeypatch.setattr(
        document_retrieval,
        "search_documents",
        Mock(side_effect=RetrievalError("qdrant_unavailable", "知识库不可用。")),
    )
    rag_model.ainvoke.side_effect = [
        tool_call(),
        AIMessage(content="知识库暂时不可用，请稍后再试。"),
    ]
    result = await agent.compile().ainvoke({"messages": [HumanMessage(content="查知识库")]})
    payload = json.loads(result["messages"][-2].content)
    assert payload["status"] == "error"
    assert payload["error_code"] == "qdrant_unavailable"
    assert not result["messages"][-1].tool_calls


@pytest.mark.asyncio
async def test_tool_loop_is_bounded_and_resets_on_next_turn(rag_model, monkeypatch):
    search = Mock(return_value=[])
    monkeypatch.setattr(document_retrieval, "search_documents", search)
    rag_model.ainvoke.side_effect = [
        tool_call(1),
        tool_call(2),
        tool_call(3),
        tool_call(4),
        AIMessage(content="暂无资料。"),
    ]
    graph = agent.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "budget"}}
    first = await graph.ainvoke({"messages": [HumanMessage(content="查询")]}, config)
    assert search.call_count == 2
    assert "预算" in first["messages"][-1].content
    assert not first["messages"][-1].tool_calls
    second = await graph.ainvoke({"messages": [HumanMessage(content="换个问题")]}, config)
    assert search.call_count == 3
    assert second["tool_rounds"] == 1


@pytest.mark.asyncio
async def test_expired_turn_does_not_call_model(rag_model):
    result = await respond(
        {"messages": [HumanMessage(content="问题")], "deadline": monotonic() - 1}, {}
    )
    assert "超时" in result["messages"][0].content
    rag_model.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_tools_get_matching_tool_message(rag_model, monkeypatch):
    search = Mock(return_value=[])
    monkeypatch.setattr(document_retrieval, "search_documents", search)
    result = await run_tools({"messages": [tool_call()], "deadline": monotonic() - 1}, {})
    assert result["messages"][0].tool_call_id == "call-1"
    assert json.loads(result["messages"][0].content)["status"] == "error"
    search.assert_not_called()
