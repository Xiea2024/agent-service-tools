"""Session authentication and tenant selection, including the whole SSE response."""

import asyncio
from contextlib import closing
from contextvars import ContextVar
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, SecretStr, field_validator
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse

from core import settings
from tenancy.security import public_settings, rate_limit
from tenancy.store import BusinessStore, Principal, password_hash, password_matches

principal_context: ContextVar[Principal | None] = ContextVar("tenant_principal", default=None)
store_context: ContextVar[BusinessStore] = ContextVar("tenant_store")
COOKIE = "supportpilot_session"
router = APIRouter()
_DUMMY_PASSWORD_HASH = password_hash("missing-user-password")
_KDF = asyncio.Semaphore(1)


async def hash_password(value):
    async with _KDF:
        return await asyncio.to_thread(password_hash, value)


def current_principal() -> Principal | None:
    return principal_context.get()


def business_store():
    return store_context.get()


class TenantMiddleware:
    """Pure ASGI middleware keeps identity bound until the stream finishes.

    Tenant mode exposes only the audited SupportPilot API. Shared AUTH_SECRET,
    AG-UI, sample agents and the legacy feedback endpoint cannot bypass ownership.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not settings.TENANT_MODE:
            await self.app(scope, receive, send)
            return
        request = StarletteRequest(scope)
        path = scope["path"]
        principal_token = store_token = gateway_token = None
        try:
            policy = public_settings()
            if policy.PUBLIC_DEMO and request.headers.get("host") != policy.PUBLIC_HOST:
                if path != "/health":
                    raise HTTPException(400, "无效的请求主机")
            origin = request.headers.get("origin")
            allowed = {v.strip() for v in settings.TENANT_ALLOWED_ORIGINS.split(",") if v.strip()}
            allowed.add(str(request.base_url).rstrip("/"))
            if policy.PUBLIC_DEMO:
                allowed = {policy.PUBLIC_ORIGIN}
                if request.method not in {"GET", "HEAD", "OPTIONS"} and origin not in allowed:
                    raise HTTPException(403, "请从本站提交请求")
            if (
                request.method not in {"GET", "HEAD", "OPTIONS"}
                and origin
                and origin not in allowed
            ):
                raise HTTPException(403, "不允许的请求来源")
            if request.headers.get("sec-fetch-site") == "cross-site" and request.method not in {
                "GET",
                "HEAD",
                "OPTIONS",
            }:
                raise HTTPException(403, "不允许跨站提交")
            store = scope["app"].state.business
            store_token = store_context.set(store)
            if path not in {
                "/health",
                "/auth/config",
                "/auth/login",
                "/auth/register",
                "/auth/verify",
                "/auth/forgot",
                "/auth/reset",
            }:
                auth = request.headers.get("authorization", "")
                token = (
                    auth[7:]
                    if auth.lower().startswith("bearer ")
                    else request.cookies.get(COOKIE, "")
                )
                principal, _ = await store.authenticate(token, request.headers.get("x-tenant-id"))
                principal_token = principal_context.set(principal)
                allowed_paths = {
                    "/info",
                    "/auth/me",
                    "/auth/logout",
                    "/auth/logout-all",
                    "/tenants",
                    "/tenants/members",
                    "/knowledge/documents",
                    "/invoke",
                    "/stream",
                    "/history",
                    "/threads",
                    "/knowledge/documents/import",
                    "/dev-issue-agent/invoke",
                    "/dev-issue-agent/stream",
                    "/dev-issue-agent/history",
                    "/dev-issue-agent/threads",
                }
                if path not in allowed_paths and not path.startswith(("/runs/", "/admin/users/")):
                    raise HTTPException(404, "该入口尚未支持多租户")
                if policy.PUBLIC_DEMO and path in {
                    "/invoke",
                    "/stream",
                    "/dev-issue-agent/invoke",
                    "/dev-issue-agent/stream",
                }:
                    from tenancy.gateway import gateway_key, user_gateway_key

                    if not policy.PAID_CALLS_ENABLED:
                        raise HTTPException(503, "模型服务暂停")
                    gateway_token = gateway_key.set(await user_gateway_key(store, principal))
            await self.app(scope, receive, send)
        except HTTPException as exc:
            await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(
                scope, receive, send
            )
        finally:
            if gateway_token is not None:
                from tenancy.gateway import gateway_key

                gateway_key.reset(gateway_token)
            if principal_token is not None:
                principal_context.reset(principal_token)
            if store_token is not None:
                store_context.reset(store_token)


class EmailInput(BaseModel):
    model_config = {"extra": "forbid"}
    email: str = Field(min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value):
        value = value.strip().lower()
        if "@" not in value or any(c.isspace() for c in value):
            raise ValueError("请输入有效邮箱")
        return value


class LoginInput(EmailInput):
    password: SecretStr = Field(min_length=1, max_length=256)


class TeamInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("团队名称不能为空")
        return value.strip()


class MemberInput(EmailInput):
    password: SecretStr | None = Field(default=None, min_length=10, max_length=256)
    role: Literal["admin", "member"] = "member"


class FeedbackInput(BaseModel):
    helpful: bool
    comment: str = Field(default="", max_length=2000)


def require_tenant():
    p = current_principal()
    if not settings.TENANT_MODE or not p:
        raise HTTPException(404, "多租户模式未启用")
    return p


@router.get("/auth/config")
async def auth_config():
    policy = public_settings()
    return {
        "enabled": settings.TENANT_MODE,
        "public_demo": policy.PUBLIC_DEMO,
        "registration_enabled": policy.registration_available(),
        "turnstile_site_key": policy.TURNSTILE_SITE_KEY,
    }


@router.post("/auth/login")
async def login(input: LoginInput, response: Response, request: Request):
    if not settings.TENANT_MODE:
        raise HTTPException(404)
    store = business_store()
    if public_settings().PUBLIC_DEMO:
        await rate_limit("login-ip", request.client.host if request.client else "unknown", 20, 600)
        await rate_limit("login-email", input.email, 10, 600)
    user = await store.login_user(input.email)
    # Missing users still perform the expensive KDF, avoiding the obvious timing leak.
    encoded = user["password_hash"] if user else _DUMMY_PASSWORD_HASH
    async with _KDF:
        valid = await asyncio.to_thread(
            password_matches, input.password.get_secret_value(), encoded
        )
    if not user or not valid or user.get("disabled") or not user.get("email_verified", True):
        raise HTTPException(401, "邮箱或密码不正确")
    if encoded.startswith("scrypt$"):
        updated = await hash_password(input.password.get_secret_value())
        async with store.pool.connection() as db:
            await db.execute(
                "UPDATE sp_users SET password_hash=%s WHERE id=%s AND password_hash=%s",
                (updated, user["id"], encoded),
            )
    token = await store.create_session(user["id"])
    response.set_cookie(
        COOKIE,
        token,
        max_age=43200,
        httponly=True,
        secure=settings.TENANT_COOKIE_SECURE,
        samesite="strict",
        path="/",
    )
    _, profile = await store.authenticate(token)
    return profile


@router.get("/auth/me")
async def me(request: Request):
    require_tenant()
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else request.cookies.get(COOKIE, "")
    _, profile = await business_store().authenticate(token, request.headers.get("x-tenant-id"))
    return profile


@router.post("/auth/logout")
async def logout(request: Request, response: Response):
    require_tenant()
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else request.cookies.get(COOKIE, "")
    await business_store().revoke_session(token)
    response.delete_cookie(COOKIE, path="/")
    return {"status": "ok"}


@router.post("/tenants", status_code=201)
async def create_team(input: TeamInput):
    p = require_tenant()
    if public_settings().PUBLIC_DEMO and not p.platform_admin:
        raise HTTPException(403, "仅平台管理员可以创建团队")
    return await business_store().create_team(p, input.name)


@router.get("/tenants/members")
async def members():
    p = require_tenant()
    return {"members": await business_store().members(p)}


@router.post("/tenants/members", status_code=201)
async def add_member(input: MemberInput):
    p = require_tenant()
    if p.role != "admin":
        raise HTTPException(403, "需要团队管理员权限")
    if public_settings().PUBLIC_DEMO and (
        input.password or (input.role == "admin" and not p.platform_admin)
    ):
        raise HTTPException(403, "请添加已注册成员；管理角色仅平台管理员可授予")
    encoded = (
        await asyncio.to_thread(password_hash, input.password.get_secret_value())
        if input.password
        else None
    )
    await business_store().add_member(p, input.email.strip().lower(), encoded, input.role)
    return {"status": "ok"}


@router.get("/knowledge/documents")
async def documents():
    p = require_tenant()
    return {"documents": await business_store().documents(p)}


@router.post("/knowledge/documents/import", status_code=201)
async def import_document(request: Request):
    p = require_tenant()
    if p.role != "admin":
        raise HTTPException(403, "需要团队管理员权限")
    # Bound upload before JSON parsing; never accept a tenant ID in the file as authority.
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 10 * 1024 * 1024:
            raise HTTPException(413, "向量文件不能超过 10 MB")
    from pydantic import ValidationError

    from document_vectors import DocumentVectors

    try:
        data = DocumentVectors.model_validate_json(bytes(raw))
    except ValidationError:
        raise HTTPException(422, "向量文件格式不合法，请使用入库脚本生成 vectors.json") from None

    def upload():
        from qdrant_store import QdrantSettings, create_qdrant_client, index_vectors

        config = QdrantSettings()
        if public_settings().PUBLIC_DEMO:
            from embedding_client import EmbeddingSettings

            embedding = EmbeddingSettings()
            if (
                data.embedding_model != embedding.EMBEDDING_MODEL
                or data.embedding_base_url.rstrip("/")
                != str(embedding.EMBEDDING_BASE_URL).rstrip("/")
                or data.dimension != public_settings().DEMO_EMBEDDING_DIMENSION
            ):
                raise ValueError("Embedding 配置必须与服务端一致")
        with closing(create_qdrant_client(config)) as client:
            return index_vectors(client, config.QDRANT_COLLECTION, data, tenant_id=p.tenant_id)

    try:
        count = await business_store().import_document(p, data, upload)
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(409, "索引配置或文档版本冲突，请检查后端日志与索引配置") from None
    except Exception:
        raise HTTPException(503, "知识库入库失败，可使用相同文件重试") from None
    return {"document_id": data.batch.document.document_id, "chunk_count": count}


@router.get("/runs/{run_id}")
async def run(run_id: UUID):
    p = require_tenant()
    return await business_store().run(p, run_id)


@router.post("/runs/{run_id}/feedback", status_code=201)
async def feedback(run_id: UUID, input: FeedbackInput):
    p = require_tenant()
    await business_store().feedback(p, run_id, input.helpful, input.comment)
    return {"status": "ok"}
