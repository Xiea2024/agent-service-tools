"""Run against an explicitly supplied disposable PostgreSQL, never the app DB.

TEST_POSTGRES_DSN=postgresql://... pytest tests/test_tenancy_postgres.py
Each test owns a random schema, which is removed after the test.
"""

import asyncio
import os
import sys
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from tenancy.store import BusinessStore, Principal, password_hash, password_matches

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_DSN"), reason="set TEST_POSTGRES_DSN to an isolated PostgreSQL"
)


@pytest.fixture(scope="session")
def event_loop_policy():
    return (
        asyncio.WindowsSelectorEventLoopPolicy()
        if sys.platform == "win32"
        else asyncio.DefaultEventLoopPolicy()
    )


@pytest_asyncio.fixture
async def store():
    dsn = os.environ["TEST_POSTGRES_DSN"]
    schema = "sp_test_" + uuid4().hex
    async with await AsyncConnection.connect(dsn, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        async with AsyncConnectionPool(
            dsn,
            min_size=1,
            max_size=5,
            open=False,
            kwargs={"row_factory": dict_row, "options": f"-c search_path={schema}"},
        ) as pool:
            await pool.wait(timeout=10)
            store = BusinessStore(pool)
            await store.migrate()
            await store.migrate()
            yield store
    finally:
        async with await AsyncConnection.connect(dsn, autocommit=True) as admin:
            await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


async def account(store, name):
    tenant = await store.create_account(
        f"{name}@example.com", password_hash("test-password-123"), name
    )
    user = await store.login_user(f"{name}@example.com")
    token = await store.create_session(user["id"])
    principal, _ = await store.authenticate(token, tenant)
    return principal, token


@pytest_asyncio.fixture
async def postgres_saver(store):
    async with store.pool.connection() as db:
        await db.set_autocommit(True)
        saver = AsyncPostgresSaver(db)
        await saver.setup()
        yield saver
        await db.set_autocommit(False)


@pytest.mark.asyncio
async def test_migrations_rollback_ownership_conflicts_and_feedback(store):
    a, at = await account(store, "a")
    b, _ = await account(store, "b")
    with pytest.raises(HTTPException) as denied:
        await store.authenticate(at, b.tenant_id)
    assert denied.value.status_code == 403
    internal = await store.thread(a, "external", "dev-issue-agent", title="问题")
    assert internal != "external"
    for other in (b, Principal(b.user_id, a.tenant_id, "member")):
        with pytest.raises(HTTPException):
            await store.thread(other, "external", "dev-issue-agent", title="伪造")
        assert await store.threads(other, "dev-issue-agent", 100) == []
    run = uuid4()
    await store.start_run(a, run, "external", "问题")
    with pytest.raises(HTTPException) as conflict:
        await store.start_run(a, uuid4(), "external", "并发问题")
    assert conflict.value.status_code == 409
    await store.finish_run(a, run, "succeeded", "回答", [{"source": "真实工具"}])
    await store.finish_run(a, run, "failed")
    assert (await store.run(a, run))["status"] == "succeeded"
    for operation in (store.run(b, run), store.feedback(b, run, True, "越权")):
        with pytest.raises(HTTPException) as denied:
            await operation
        assert denied.value.status_code == 404
    await store.feedback(a, run, True, "有效反馈")
    with pytest.raises(HTTPException) as duplicate:
        await store.feedback(a, run, False, "覆盖")
    assert duplicate.value.status_code == 409
    assert (await store.run(a, run))["helpful"] is True
    await store.revoke_session(at)
    with pytest.raises(HTTPException):
        await store.authenticate(at)


@pytest.mark.asyncio
async def test_http_login_team_switch_stream_history_and_bypass_rejection(
    store, postgres_saver, monkeypatch
):
    from core import settings
    from service import service

    a, _ = await account(store, "a")
    b, bt = await account(store, "b")
    monkeypatch.setattr(settings, "TENANT_MODE", True)
    monkeypatch.setattr(settings, "TENANT_COOKIE_SECURE", False)
    monkeypatch.setattr(service.app.state, "business", store, raising=False)
    received = []

    async def reply(state, config):
        received.append(config["configurable"])
        return {"messages": [AIMessage(content="隔离后的回答")]}

    graph = StateGraph(MessagesState)
    graph.add_node("reply", reply)
    graph.add_edge(START, "reply")
    graph.add_edge("reply", END)
    compiled = graph.compile(checkpointer=postgres_saver)
    monkeypatch.setattr(service, "get_agent", lambda _: compiled)
    transport = httpx.ASGITransport(app=service.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/info")).status_code == 401
        assert (
            await client.get("/info", headers={"Authorization": "Bearer shared-secret"})
        ).status_code == 401
        assert (
            await client.post("/auth/login", json={"email": "a@example.com", "password": "wrong"})
        ).status_code == 401
        login = await client.post(
            "/auth/login", json={"email": "a@example.com", "password": "test-password-123"}
        )
        assert login.status_code == 200
        assert "httponly" in login.headers["set-cookie"].lower()
        assert (await client.get("/auth/me")).json()["tenant_id"] == a.tenant_id
        assert (
            await client.get("/auth/me", headers={"X-Tenant-ID": b.tenant_id})
        ).status_code == 403
        for path in ("/agui/run", "/chatbot/invoke", "/feedback"):
            assert (await client.post(path, json={})).status_code == 404
        assert (
            await client.post(
                "/tenants", json={"name": "evil"}, headers={"Origin": "https://evil.example"}
            )
        ).status_code == 403
        forged = await client.post(
            "/dev-issue-agent/stream",
            json={"message": "伪造", "agent_config": {"tenant_id": b.tenant_id}},
        )
        assert forged.status_code == 422
        stream = await client.post(
            "/stream", json={"message": "你好", "thread_id": "public-thread", "user_id": b.user_id}
        )
        assert stream.status_code == 200 and "[DONE]" in stream.text
        assert received[-1]["tenant_id"] == a.tenant_id
        assert received[-1]["user_id"] == a.memory_user_id
        assert received[-1]["thread_id"] != "public-thread"
        threads = (await client.get("/threads")).json()["threads"]
        assert threads[0]["thread_id"] == "public-thread"
        history = await client.post("/history", json={"thread_id": "public-thread"})
        assert history.status_code == 200 and len(history.json()["messages"]) == 2
        # Rebuild both repositories to model a new service instance reading durable data.
        replacement = BusinessStore(store.pool)
        assert (await replacement.threads(a, "dev-issue-agent", 10))[0][
            "thread_id"
        ] == "public-thread"
        async with store.pool.connection() as fresh_db:
            fresh_graph = graph.compile(checkpointer=AsyncPostgresSaver(fresh_db))
            recovered = await fresh_graph.aget_state(
                {"configurable": {"thread_id": received[-1]["thread_id"]}}
            )
            assert recovered.values["messages"][-1].content == "隔离后的回答"
        import json

        events = [
            json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: {")
        ]
        run_id = next(e["content"]["run_id"] for e in events if e["type"] == "message")
        assert (await client.get(f"/runs/{run_id}")).json()["answer"] == "隔离后的回答"
        assert (
            await client.post(f"/runs/{run_id}/feedback", json={"helpful": True})
        ).status_code == 201
        new = (await client.post("/tenants", json={"name": "另一团队"})).json()
        client.headers["X-Tenant-ID"] = new["id"]
        assert (await client.get("/threads")).json()["threads"] == []
        assert (
            await client.post("/history", json={"thread_id": "public-thread"})
        ).status_code == 404
        assert (
            await client.post("/stream", json={"message": "猜测", "thread_id": "public-thread"})
        ).status_code == 404
        assert (await client.get(f"/runs/{run_id}")).status_code == 404
        assert (
            await client.post(f"/runs/{run_id}/feedback", json={"helpful": False})
        ).status_code == 404
        client.headers.clear()
        assert (await client.post("/auth/logout")).status_code == 200
        assert (await client.get("/auth/me")).status_code == 401
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {bt}"}
    ) as client:
        assert (await client.get("/threads")).json()["threads"] == []
        assert (
            await client.post("/history", json={"thread_id": "public-thread"})
        ).status_code == 404


@pytest.mark.asyncio
async def test_member_cannot_provision_users_or_import_and_expiry_is_enforced(store, monkeypatch):
    from core import settings
    from service import service

    a, _ = await account(store, "admin")
    b, token = await account(store, "member")
    await store.add_member(a, "member@example.com", None, "member")
    p, _ = await store.authenticate(token, a.tenant_id)
    assert p.role == "member"
    with pytest.raises(HTTPException):
        await store.add_member(p, "evil@example.com", password_hash("evil-password-123"), "admin")
    assert await store.login_user("evil@example.com") is None
    monkeypatch.setattr(settings, "TENANT_MODE", True)
    monkeypatch.setattr(service.app.state, "business", store, raising=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=service.app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}", "X-Tenant-ID": a.tenant_id},
    ) as client:
        assert (
            await client.post(
                "/tenants/members", json={"email": "x@example.com", "password": "new-password-123"}
            )
        ).status_code == 403
        assert (await client.post("/knowledge/documents/import", content="{}")).status_code == 403
        assert (await client.get("/knowledge/documents")).json() == {"documents": []}
    async with store.pool.connection() as db:
        await db.execute(
            "UPDATE sp_sessions SET expires_at=now()-interval '1 second' WHERE user_id=%s",
            (b.user_id,),
        )
    with pytest.raises(HTTPException):
        await store.authenticate(token)


def test_password_hashing():
    encoded = password_hash("valid-password-123")
    assert "valid-password-123" not in encoded
    assert password_matches("valid-password-123", encoded)
    assert not password_matches("wrong", encoded)


@pytest.mark.asyncio
async def test_document_import_catalog_retry_conflict_and_tenant_isolation(store, monkeypatch):
    from unittest.mock import Mock

    from qdrant_client import QdrantClient
    from test_document_retrieval import configs
    from test_document_vectors import make_batch, make_vectors

    from core import settings
    from document_retrieval import query_index
    from service import service

    a, at = await account(store, "a")
    b, bt = await account(store, "b")
    monkeypatch.setattr(settings, "TENANT_MODE", True)
    monkeypatch.setattr(service.app.state, "business", store, raising=False)
    qdrant = QdrantClient(":memory:")
    close = qdrant.close
    monkeypatch.setattr(qdrant, "close", lambda: None)
    monkeypatch.setattr("qdrant_store.create_qdrant_client", lambda _: qdrant)
    monkeypatch.setattr("qdrant_store.QdrantSettings", lambda: configs()[1])
    data = make_vectors()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service.app), base_url="http://test"
        ) as client:
            client.headers["Authorization"] = f"Bearer {at}"
            assert (
                await client.post("/knowledge/documents/import", content="{}")
            ).status_code == 422
            assert (
                await client.post("/knowledge/documents/import", content=data.model_dump_json())
            ).status_code == 201
            assert (
                await client.post("/knowledge/documents/import", content=data.model_dump_json())
            ).status_code == 201
            assert len((await client.get("/knowledge/documents")).json()["documents"]) == 1
            changed = make_vectors(make_batch("改变后的正文。" * 30))
            assert (
                await client.post("/knowledge/documents/import", content=changed.model_dump_json())
            ).status_code == 409
            client.headers["Authorization"] = f"Bearer {bt}"
            assert (await client.get("/knowledge/documents")).json()["documents"] == []
            assert (
                await client.post("/knowledge/documents/import", content=data.model_dump_json())
            ).status_code == 201
        assert qdrant.count("docs").count == len(data.vectors) * 2
        embedding = Mock()
        embedding.embed_query.return_value = data.vectors[0]
        args = ("问题", qdrant, embedding, *configs())
        assert query_index(
            *args, tenant_id=a.tenant_id, document_ids=[data.batch.document.document_id]
        )
        assert query_index(*args, tenant_id=b.tenant_id, document_ids=["not-registered"]) == []
        assert query_index(*args, tenant_id=a.tenant_id, document_ids=[]) == []
        # Failed uploads leave no catalog entry; a later identical retry can succeed.
        c, _ = await account(store, "c")

        def fail():
            raise RuntimeError("simulated qdrant outage")

        with pytest.raises(RuntimeError):
            await store.import_document(c, data, fail)
        assert await store.documents(c) == []
        from qdrant_store import index_vectors

        await store.import_document(
            c, data, lambda: index_vectors(qdrant, "docs", data, tenant_id=c.tenant_id)
        )
        assert len(await store.documents(c)) == 1
    finally:
        close()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
async def test_running_conflict_cancellation_and_failure_are_persisted(
    store, monkeypatch, endpoint
):
    from core import settings
    from service import service

    p, token = await account(store, "a")
    monkeypatch.setattr(settings, "TENANT_MODE", True)
    monkeypatch.setattr(service.app.state, "business", store, raising=False)
    entered, block = asyncio.Event(), asyncio.Event()

    async def reply(state):
        question = state["messages"][-1].content
        if question == "cancel":
            entered.set()
            await block.wait()
        if question == "explode":
            raise RuntimeError("synthetic model failure")
        return {"messages": [AIMessage(content="完成")]}

    graph = StateGraph(MessagesState)
    graph.add_node("reply", reply)
    graph.add_edge(START, "reply")
    graph.add_edge("reply", END)
    monkeypatch.setattr(service, "get_agent", lambda _: compiled)
    compiled = graph.compile(checkpointer=MemorySaver())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=service.app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        task = asyncio.create_task(
            client.post(f"/{endpoint}", json={"message": "cancel", "thread_id": "cancel-thread"})
        )
        await asyncio.wait_for(entered.wait(), 5)
        denied = await client.post(
            f"/{endpoint}", json={"message": "second", "thread_id": "cancel-thread"}
        )
        assert denied.status_code == 409
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with store.pool.connection() as db:
            cancelled = await (
                await db.execute("SELECT id,status FROM sp_runs WHERE question='cancel'")
            ).fetchone()
        assert cancelled["status"] == "interrupted"
        recovered = await client.post(
            f"/{endpoint}", json={"message": "recover", "thread_id": "cancel-thread"}
        )
        assert recovered.status_code == 200
        failed = await client.post(
            f"/{endpoint}", json={"message": "explode", "thread_id": "fail-thread"}
        )
        assert failed.status_code == (500 if endpoint == "invoke" else 200)
        if endpoint == "stream":
            assert '"type": "error"' in failed.text and "[DONE]" in failed.text
        async with store.pool.connection() as db:
            row = await (
                await db.execute("SELECT status FROM sp_runs WHERE question='explode'")
            ).fetchone()
        assert row["status"] == "failed"


@pytest.mark.asyncio
async def test_real_service_lifespan_postgres_and_builtin_fake_model(store, monkeypatch):
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from pydantic import SecretStr

    from core import settings
    from schema.models import FakeModelName
    from service import service

    async with store.pool.connection() as db:
        schema = (await (await db.execute("SELECT current_schema() AS name")).fetchone())["name"]
    dsn = os.environ["TEST_POSTGRES_DSN"]
    parsed = conninfo_to_dict(dsn)
    for field, value in {
        "TENANT_MODE": True,
        "TENANT_COOKIE_SECURE": False,
        "DATABASE_TYPE": "postgres",
        "POSTGRES_USER": parsed.get("user"),
        "POSTGRES_PASSWORD": SecretStr(parsed.get("password", "")),
        "POSTGRES_HOST": parsed.get("host"),
        "POSTGRES_PORT": int(parsed.get("port", "5432")),
        "POSTGRES_DB": parsed.get("dbname"),
        "LANGFUSE_TRACING": False,
        "DEFAULT_MODEL": FakeModelName.FAKE,
        "AVAILABLE_MODELS": {FakeModelName.FAKE},
    }.items():
        monkeypatch.setattr(settings, field, value)
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.setattr(
        "memory.postgres.get_postgres_connection_string",
        lambda: make_conninfo(dsn, options=f"-c search_path={schema}"),
    )
    # Lifespan attaches persistence to the shared graph; restore it after this test.
    graph = service.get_agent("dev-issue-agent")
    monkeypatch.setattr(graph, "checkpointer", graph.checkpointer)
    monkeypatch.setattr(graph, "store", graph.store)
    async with service.lifespan(service.app):
        await service.app.state.business.create_account(
            "live@example.com", password_hash("live-password-123"), "演示团队"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service.app), base_url="http://test"
        ) as client:
            assert (
                await client.post(
                    "/auth/login",
                    json={"email": "live@example.com", "password": "live-password-123"},
                )
            ).status_code == 200
            result = await client.post(
                "/invoke", json={"message": "你好", "thread_id": "live-thread"}
            )
            assert result.status_code == 200
            assert "fake model" in result.json()["content"]
            run = await client.get(f"/runs/{result.json()['run_id']}")
            assert run.json()["status"] == "succeeded"
            history = await client.post("/history", json={"thread_id": "live-thread"})
            assert len(history.json()["messages"]) == 2
