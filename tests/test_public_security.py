# ruff: noqa: F811
"""Security regressions use disposable PostgreSQL schemas and real Redis."""

import asyncio
import os
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError
from test_tenancy_postgres import account, event_loop_policy, store  # noqa: F401

from tenancy import registration, security
from tenancy.gateway import gateway_key, user_gateway_key
from tenancy.registration import RegisterInput
from tenancy.security import PublicSettings
from tenancy.store import Principal

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_DSN"), reason="disposable database required"
)


@pytest_asyncio.fixture
async def public(store, monkeypatch):
    from core import settings
    from service import service

    owner, _ = await account(store, "owner")
    monkeypatch.setenv("PUBLIC_DEMO", "true")
    monkeypatch.setenv("REGISTRATION_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_HOST", "demo.example.com")
    monkeypatch.setenv("PUBLIC_ORIGIN", "https://demo.example.com")
    monkeypatch.setenv("DEMO_TENANT_ID", owner.tenant_id)
    monkeypatch.setenv("RESEND_API_KEY", "test-only")
    monkeypatch.setenv("MAIL_FROM", "demo@example.com")
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "test-site")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "test-secret")
    monkeypatch.setattr(settings, "TENANT_MODE", True)
    monkeypatch.setattr(settings, "TENANT_COOKIE_SECURE", True)
    monkeypatch.setattr(service.app.state, "business", store, raising=False)
    security.public_settings.cache_clear()
    counters = {}

    async def limited(bucket, identity, limit, seconds):
        key = (bucket, identity)
        counters[key] = counters.get(key, 0) + 1
        if counters[key] > limit:
            raise HTTPException(429)

    monkeypatch.setattr(security, "rate_limit", limited)
    monkeypatch.setattr(registration, "rate_limit", limited)
    monkeypatch.setattr("tenancy.api.rate_limit", limited)
    try:
        yield store, owner, service.app
    finally:
        security.public_settings.cache_clear()


@pytest.mark.asyncio
async def test_registration_verification_reset_disable_and_no_privileges(public, monkeypatch):
    store, owner, app = public
    sent = []

    async def human(_):
        pass

    monkeypatch.setattr(registration, "verify_human", human)
    original = httpx.AsyncClient

    def external(request):
        import json

        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "test"})

    monkeypatch.setattr(
        registration.httpx,
        "AsyncClient",
        lambda **kwargs: original(**({"transport": httpx.MockTransport(external)} | kwargs)),
    )
    async with original(
        transport=httpx.ASGITransport(app=app),
        base_url="https://demo.example.com",
        headers={"Origin": "https://demo.example.com"},
    ) as client:
        payload = {"email": "new@example.com", "password": "test-password-123", "human_token": "ok"}
        assert (
            await client.post("/auth/register", json=payload | {"role": "admin"})
        ).status_code == 422
        assert (await client.post("/auth/register", json=payload)).status_code == 200
        assert await store.login_user(payload["email"]) is None
        code = sent[-1]["text"].split("\n\n")[1]
        async with store.pool.connection() as db:
            row = await (await db.execute("SELECT token_hash FROM sp_email_tokens")).fetchone()
            assert code != row["token_hash"]
        assert (await client.post("/auth/verify", json={"token": code})).status_code == 200
        assert (await client.post("/auth/verify", json={"token": code})).status_code == 400
        login = await client.post(
            "/auth/login", json={"email": payload["email"], "password": payload["password"]}
        )
        assert login.status_code == 200
        assert "Secure" in login.headers["set-cookie"] and "HttpOnly" in login.headers["set-cookie"]
        profile = login.json()
        assert profile["platform_admin"] is False and profile["tenants"][0]["role"] == "member"
        assert (await client.post("/tenants", json={"name": "hack"})).status_code == 403
        assert (
            await client.post(
                "/tenants/members", json={"email": "evil@example.com", "role": "admin"}
            )
        ).status_code == 403
        assert (await client.post("/knowledge/documents/import", content="{}")).status_code == 403
        assert (
            await client.patch(f"/admin/users/{profile['user_id']}", json={"platform_admin": True})
        ).status_code == 403
        assert (
            await client.post("/auth/forgot", json={"email": payload["email"], "human_token": "ok"})
        ).status_code == 200
        reset_code = sent[-1]["text"].split("\n\n")[1]
        assert (
            await client.post(
                "/auth/reset", json={"token": reset_code, "password": "updated-password-123"}
            )
        ).status_code == 200
        assert (await client.get("/auth/me")).status_code == 401
        assert (
            await client.post(
                "/auth/login", json={"email": payload["email"], "password": "updated-password-123"}
            )
        ).status_code == 200
        admin = Principal(owner.user_id, owner.tenant_id, "admin", True)
        await store.manage_user(admin, profile["user_id"], disabled=True)
        assert (await client.get("/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_origin_host_login_limits_and_closed_registration(public):
    _, _, app = public
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://demo.example.com"
    ) as client:
        payload = {"email": "missing@example.com", "password": "wrong"}
        assert (await client.post("/auth/login", json=payload)).status_code == 403
        assert (
            await client.post("/auth/login", json=payload, headers={"Origin": "https://evil.test"})
        ).status_code == 403
        assert (await client.get("/auth/config", headers={"Host": "evil.test"})).status_code == 400
        for _ in range(10):
            assert (
                await client.post(
                    "/auth/login", json=payload, headers={"Origin": "https://demo.example.com"}
                )
            ).status_code == 401
        assert (
            await client.post(
                "/auth/login", json=payload, headers={"Origin": "https://demo.example.com"}
            )
        ).status_code == 429
        security.public_settings().REGISTRATION_ENABLED = False
        assert (
            await client.post(
                "/auth/register",
                json=payload | {"password": "test-password-123", "human_token": "ok"},
                headers={"Origin": "https://demo.example.com"},
            )
        ).status_code == 503


@pytest.mark.asyncio
async def test_atomic_quota_across_threads_and_global(public):
    store, owner, _ = public
    for thread in ("one", "two"):
        await store.thread(owner, thread, "dev-issue-agent", title="question")
    results = await asyncio.gather(
        *(store.start_run(owner, uuid4(), t, "q") for t in ("one", "two")), return_exceptions=True
    )
    assert sum(isinstance(r, HTTPException) and r.status_code == 429 for r in results) == 1
    async with store.pool.connection() as db:
        await db.execute("UPDATE sp_runs SET status='failed'")
    security.public_settings().DEMO_DAILY_QUESTIONS = 1
    with pytest.raises(HTTPException) as exc:
        await store.start_run(owner, uuid4(), "two", "q")
    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_gateway_keys_are_encrypted_reused_and_restricted(public, monkeypatch):
    from cryptography.fernet import Fernet

    from tenancy import gateway

    store, owner, _ = public
    policy = security.public_settings()
    policy.GATEWAY_ENCRYPTION_KEY = SecretStr(Fernet.generate_key().decode())
    policy.GATEWAY_MASTER_KEY = SecretStr("sk-test-master")
    calls = []

    def upstream(request):
        import json

        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"key": "sk-user-secret"})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        gateway.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(upstream), **kwargs),
    )
    assert await user_gateway_key(store, owner) == "sk-user-secret"
    assert await user_gateway_key(store, owner) == "sk-user-secret"
    assert len(calls) == 1 and calls[0]["models"] == ["demo-chat", "demo-embedding"]
    async with store.pool.connection() as db:
        saved = await (await db.execute("SELECT encrypted_key FROM sp_gateway_keys")).fetchone()
        assert "sk-user-secret" not in saved["encrypted_key"]
    assert gateway_key.get() is None


def test_registration_fields_reject_escalation():
    with pytest.raises(ValidationError):
        RegisterInput(
            email="a@example.com",
            password="test-password-123",
            human_token="ok",
            platform_admin=True,
        )


def test_public_production_rejects_unsafe_settings():
    from types import SimpleNamespace

    with pytest.raises(ValueError):
        PublicSettings(_env_file=None, PUBLIC_DEMO=True).validate_production(
            SimpleNamespace(TENANT_MODE=False, TENANT_COOKIE_SECURE=False)
        )


@pytest.mark.asyncio
async def test_redis_rate_limit_is_atomic_and_fails_closed(monkeypatch):
    from redis.asyncio import Redis

    url = os.getenv("SUPPORTPILOT_TEST_REDIS_URL")
    if not url:
        pytest.skip("disposable Redis required")
    client = Redis.from_url(url)
    monkeypatch.setattr(security, "security_redis", lambda: client)
    try:
        results = await asyncio.gather(
            *(security.rate_limit("test", "unique-" + str(id(client)), 3, 60) for _ in range(8)),
            return_exceptions=True,
        )
        assert sum(r is None for r in results) == 3
        assert sum(isinstance(r, HTTPException) and r.status_code == 429 for r in results) == 5
    finally:
        await client.aclose()

    class Down:
        async def eval(self, *_):
            raise ConnectionError("synthetic failure")

    monkeypatch.setattr(security, "security_redis", Down)
    with pytest.raises(HTTPException) as error:
        await security.rate_limit("test", "unavailable", 3, 60)
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_global_quota_cannot_be_bypassed_by_new_account(public):
    store, owner, _ = public
    other, _ = await account(store, "other")
    policy = security.public_settings()
    policy.DEMO_GLOBAL_DAILY_QUESTIONS = 1
    for actor, thread in [(owner, "global-a"), (other, "global-b")]:
        await store.thread(actor, thread, "dev-issue-agent", title="q")
    run = uuid4()
    await store.start_run(owner, run, "global-a", "q")
    await store.finish_run(owner, run, "succeeded", "done")
    with pytest.raises(HTTPException) as error:
        await store.start_run(other, uuid4(), "global-b", "q")
    assert error.value.status_code == 429


@pytest.mark.asyncio
async def test_human_validation_requires_matching_hostname(monkeypatch):
    original = httpx.AsyncClient
    policy = PublicSettings(
        _env_file=None, TURNSTILE_SECRET_KEY="test", PUBLIC_HOST="expected.example"
    )
    monkeypatch.setattr(security, "public_settings", lambda: policy)
    monkeypatch.setattr(
        security.httpx if hasattr(security, "httpx") else httpx,
        "AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"success": True, "hostname": "wrong.example"})
            ),
            **kwargs,
        ),
    )
    with pytest.raises(HTTPException) as error:
        await security.verify_human("test")
    assert error.value.status_code == 400
