"""飞书会话 SQL 的离线测试：验证旧线程兼容、事务、所有权和重启恢复。

仅使用临时 SQLite 与合成 ID，不读取实际聊天历史或调用飞书/模型。
"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest

from feishu_sessions import FeishuSessionStore, legacy_thread_id


def store_at(tmp_path, app="app", agent="agent"):
    return FeishuSessionStore(tmp_path / "sessions.sqlite3", app, agent)


def test_initial_thread_keeps_legacy_identity(tmp_path):
    store = store_at(tmp_path)
    first = store.current("chat", "user")
    assert first.thread_id == legacy_thread_id("app", "agent", "chat")
    assert store.current("chat", "user") == first
    assert store.chat_for_user("user") == "chat"
    assert len(store.recent("chat")) == 1


def test_new_switch_and_reopen_restore_selected_thread(tmp_path):
    store = store_at(tmp_path)
    old = store.current("chat", "user")
    new = store.change("chat", "event-new")
    assert UUID(new.thread_id).version == 4
    assert new.thread_id != old.thread_id
    assert store_at(tmp_path).current("chat").thread_id == new.thread_id
    store.change("chat", "event-switch", old.thread_id)
    reopened = store_at(tmp_path)
    assert reopened.current("chat").thread_id == old.thread_id
    assert {s.thread_id for s in reopened.recent("chat")} == {old.thread_id, new.thread_id}


def test_duplicate_event_does_not_create_or_reactivate_sessions(tmp_path):
    store = store_at(tmp_path)
    one = store.change("chat", "same-event")
    assert store.change("chat", "same-event").thread_id == one.thread_id
    two = store.change("chat", "different-event")
    assert store_at(tmp_path).change("chat", "same-event").thread_id == two.thread_id
    assert len(store.recent("chat")) == 3


def test_concurrent_duplicate_creation_is_atomic(tmp_path):
    store = store_at(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: store.change("chat", "one-event").thread_id, range(8)))
    assert len(set(ids)) == 1
    assert len(store.recent("chat")) == 2


def test_cross_chat_switch_fails_and_rolls_back(tmp_path):
    store = store_at(tmp_path)
    first, other = store.current("one", "user1"), store.current("two", "user2")
    with pytest.raises(ValueError, match="其他聊天"):
        store.change("one", "bad", other.thread_id)
    assert store.current("one").thread_id == first.thread_id
    # 失败事务没有占用事件 ID，修正目标后仍可提交。
    assert store.change("one", "bad", first.thread_id).thread_id == first.thread_id


def test_user_chat_binding_cannot_be_reassigned(tmp_path):
    store = store_at(tmp_path)
    store.current("chat", "owner")
    with pytest.raises(ValueError, match="不属于"):
        store.current("chat", "other-owner")
    assert store.chat_for_user("owner") == "chat"
    assert store.chat_for_user("other-owner") is None


def test_app_and_agent_namespaces_are_isolated(tmp_path):
    stores = [
        store_at(tmp_path),
        store_at(tmp_path, app="app2"),
        store_at(tmp_path, agent="agent2"),
    ]
    ids = [store.current("chat", "user").thread_id for store in stores]
    assert len(set(ids)) == 3
    with pytest.raises(ValueError, match="其他聊天"):
        stores[0].change("chat", "bad", ids[1])


def test_successful_turn_updates_exact_session_and_title_safely(tmp_path):
    store = store_at(tmp_path)
    one = store.change("chat", "new-1")
    two = store.change("chat", "new-2")
    title = "参数 '); DROP TABLE feishu_sessions; -- [资料](https://example.com)"
    store.record_turn("chat", one.thread_id, title)
    stored = next(s for s in store.recent("chat") if s.thread_id == one.thread_id)
    assert stored.title == title[:40]
    assert stored.turn_count == 1
    store.record_turn("chat", one.thread_id, "后续追问")
    assert store.current("chat").thread_id == two.thread_id
    assert next(s for s in store.recent("chat") if s.thread_id == one.thread_id).title == title[:40]
    with pytest.raises(ValueError):
        store.record_turn("another-chat", one.thread_id, "不能写入")


def test_conflicting_event_id_rolls_back(tmp_path):
    store = store_at(tmp_path)
    session = store.change("chat", "same")
    with pytest.raises(ValueError, match="不一致"):
        store.change("chat", "same", session.thread_id)
    assert store.current("chat").thread_id == session.thread_id
    assert len(store.recent("chat")) == 2


def test_history_limit_and_tables_are_business_records(tmp_path):
    store = store_at(tmp_path)
    for i in range(10):
        store.change("chat", f"new-{i}")
    assert len(store.recent("chat")) == 8
    assert len(store.recent("chat", 20)) == 11
    with pytest.raises(ValueError):
        store.recent("chat", 0)
    with sqlite3.connect(store.path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {"feishu_chats", "feishu_sessions", "feishu_session_actions"}


@pytest.mark.asyncio
async def test_reopening_both_databases_restores_context_for_selected_session(tmp_path):
    """真实 SQLite checkpointer 配合会话目录，关闭重开后仍恢复选中的独立历史。"""
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.graph import END, START, MessagesState, StateGraph

    def respond(state):
        text = " / ".join(m.content for m in state["messages"] if isinstance(m, HumanMessage))
        return {"messages": [AIMessage(content=text)]}

    builder = StateGraph(MessagesState)
    builder.add_node("reply", respond)
    builder.add_edge(START, "reply")
    builder.add_edge("reply", END)
    store = store_at(tmp_path)
    old = store.current("chat", "user")
    checkpoint_path = str(tmp_path / "checkpoints.sqlite3")
    async with AsyncSqliteSaver.from_conn_string(checkpoint_path) as saver:
        await saver.setup()
        graph = builder.compile(checkpointer=saver)
        await graph.ainvoke(
            {"messages": [HumanMessage(content="跨域")]},
            {"configurable": {"thread_id": old.thread_id}},
        )
        new = store.change("chat", "new")
        await graph.ainvoke(
            {"messages": [HumanMessage(content="空指针")]},
            {"configurable": {"thread_id": new.thread_id}},
        )
    store = store_at(tmp_path)
    async with AsyncSqliteSaver.from_conn_string(checkpoint_path) as saver:
        await saver.setup()
        graph = builder.compile(checkpointer=saver)
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content="继续")]},
            {"configurable": {"thread_id": store.current("chat").thread_id}},
        )
        assert result["messages"][-1].content == "空指针 / 继续"
        store.change("chat", "resume", old.thread_id)
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content="旧问题")]},
            {"configurable": {"thread_id": store.current("chat").thread_id}},
        )
        assert result["messages"][-1].content == "跨域 / 旧问题"
