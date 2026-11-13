"""supervisor 委派、两个专家的隔离，以及委派失败时的回退。

这些测试用假模型驱动真实的 LangGraph，验证的是图的行为与记录，不是模型的判断质量。
"""

from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from agents.dev_issue_agent import agent
from agents.supervisor import (
    DOC_EXPERT,
    LOG_EXPERT,
    choose_by_rules,
    parse_delegation,
)
from observability import stages

LOG_TEXT = (
    "2026-11-12 10:03:21,114 ERROR request_id=abc123def456\n"
    "Traceback (most recent call last):\n"
    '  File "/srv/app/handler.py", line 42, in handle\n'
    "ValueError: boom\n"
    "请分析这段日志。"
)
DOC_TEXT = "跨域文档在哪里？"
CONFIG = {"configurable": {"thread_id": "t"}, "metadata": {"run_id": "sup-run"}}


def delegate(target: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "delegate", "args": {"target": target}, "id": "call-delegate"}],
    )


@pytest.fixture
def fake_model(monkeypatch):
    """返回 (mock, responses)：responses 由各用例按调用顺序填入，异常对象会被抛出。"""
    responses: list[object] = []

    def next_response(*args, **kwargs):
        item = responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    mocked = Mock()
    mocked.bind_tools.return_value = mocked
    mocked.ainvoke = AsyncMock(side_effect=next_response)
    monkeypatch.setattr("agents.dev_issue_agent.get_model", Mock(return_value=mocked))
    stages.clear_stages()
    return mocked, responses


def test_parse_delegation_accepts_only_known_targets():
    assert parse_delegation(delegate(DOC_EXPERT)) == DOC_EXPERT
    assert parse_delegation(delegate(LOG_EXPERT)) == LOG_EXPERT
    assert parse_delegation(delegate("billing_expert")) is None
    assert parse_delegation(AIMessage(content="我选日志专家")) is None
    assert parse_delegation(Mock(tool_calls=[{"name": "other", "args": {}}])) is None
    assert parse_delegation(Mock(tool_calls=[{"name": "delegate", "args": "log_expert"}])) is None


def test_rules_fallback_separates_logs_from_questions():
    assert choose_by_rules(LOG_TEXT) == LOG_EXPERT
    assert choose_by_rules(DOC_TEXT) == DOC_EXPERT
    assert choose_by_rules("订单接口返回 500，怎么排查？") == DOC_EXPERT
    assert choose_by_rules("你好") == DOC_EXPERT


@pytest.mark.asyncio
async def test_log_request_goes_to_log_expert_without_doc_tools(fake_model):
    mocked, responses = fake_model
    responses.extend(
        [delegate(LOG_EXPERT), AIMessage(content="已确认 TimeoutError 发生在 handler.py:42。")]
    )
    graph = agent.compile(checkpointer=MemorySaver())
    await graph.ainvoke({"messages": [HumanMessage(content=LOG_TEXT)]}, CONFIG)
    # 只绑定过委派工具；日志专家不绑定文档检索工具。
    assert mocked.bind_tools.call_count == 1
    assert mocked.ainvoke.await_count == 2
    records = stages.stages_for("sup-run")
    route = next(record for record in records if record["stage"] == "route")
    assert route["decisions"]["target"] == LOG_EXPERT
    assert route["decisions"]["decided_by"] == "model"


@pytest.mark.asyncio
async def test_document_request_goes_to_doc_expert(fake_model):
    mocked, responses = fake_model
    responses.extend([delegate(DOC_EXPERT), AIMessage(content="请补充请求路径和状态码。")])
    graph = agent.compile(checkpointer=MemorySaver())
    await graph.ainvoke({"messages": [HumanMessage(content=DOC_TEXT)]}, CONFIG)
    # 委派工具 + 文档检索工具
    assert mocked.bind_tools.call_count == 2


@pytest.mark.asyncio
async def test_missing_decision_falls_back_to_rules(fake_model):
    mocked, responses = fake_model
    # 模型没有调用工具：按规则回退，不能把请求丢掉。
    responses.extend([AIMessage(content="我不确定。"), AIMessage(content="请补充带时间戳的日志。")])
    graph = agent.compile(checkpointer=MemorySaver())
    await graph.ainvoke({"messages": [HumanMessage(content=LOG_TEXT)]}, CONFIG)
    route = next(r for r in stages.stages_for("sup-run") if r["stage"] == "route")
    assert route["decisions"]["target"] == LOG_EXPERT
    assert route["decisions"]["decided_by"] == "rules_fallback"
    assert mocked.bind_tools.call_count == 1


@pytest.mark.asyncio
async def test_supervisor_timeout_degrades_to_doc_expert(fake_model):
    mocked, responses = fake_model
    responses.extend([TimeoutError("supervisor slow"), AIMessage(content="请补充错误信息。")])
    graph = agent.compile(checkpointer=MemorySaver())
    await graph.ainvoke({"messages": [HumanMessage(content=LOG_TEXT)]}, CONFIG)
    route = next(r for r in stages.stages_for("sup-run") if r["stage"] == "route")
    assert route["decisions"]["target"] == DOC_EXPERT
    assert route["decisions"]["decided_by"] == "timeout"
    assert route["outcome"] == "timeout"


@pytest.mark.asyncio
async def test_delegation_message_is_not_stored_in_history(fake_model):
    mocked, responses = fake_model
    responses.extend([delegate(DOC_EXPERT), AIMessage(content="请补充请求路径。")])
    graph = agent.compile(checkpointer=MemorySaver())
    await graph.ainvoke({"messages": [HumanMessage(content=DOC_TEXT)]}, CONFIG)
    snapshot = await graph.aget_state(CONFIG)
    types = [message.type for message in snapshot.values["messages"]]
    # 人类消息 + 专家回答；委派用的 tool_call 消息不能留在会话里。
    assert types == ["human", "ai"]
    stored = snapshot.values["messages"][-1]
    assert not stored.tool_calls
