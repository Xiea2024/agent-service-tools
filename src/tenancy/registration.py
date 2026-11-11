"""Email verification and password recovery; no client-supplied privileges."""

from uuid import UUID

import httpx
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, SecretStr

from tenancy.api import EmailInput, business_store, hash_password, require_tenant, router
from tenancy.security import public_settings, rate_limit, verify_human


class MailInput(EmailInput):
    human_token: str = Field(min_length=1, max_length=4096)


class RegisterInput(MailInput):
    password: SecretStr = Field(min_length=10, max_length=256)


class TokenInput(BaseModel):
    model_config = {"extra": "forbid"}
    token: str = Field(min_length=32, max_length=128)


class ResetInput(TokenInput):
    password: SecretStr = Field(min_length=10, max_length=256)


async def send_code(input, request, purpose):
    policy = public_settings()
    if not policy.registration_available():
        raise HTTPException(503, "邮箱服务尚未开放")
    await rate_limit("mail-ip", request.client.host if request.client else "unknown", 5, 3600)
    await rate_limit("mail-address", input.email, 3, 3600)
    await verify_human(input.human_token)
    await rate_limit("mail-global", "global", policy.DEMO_DAILY_EMAILS, 86400)
    encoded = (
        await hash_password(input.password.get_secret_value()) if purpose == "register" else None
    )
    store = business_store()
    user = await store.login_user(input.email)
    if (purpose == "register" and user) or (purpose == "reset" and not user):
        return {"status": "请检查邮箱；若符合条件，验证码将在邮件中发送"}
    assert policy.RESEND_API_KEY
    token = await store.email_token(input.email, purpose, encoded)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": "Bearer " + policy.RESEND_API_KEY.get_secret_value()},
                json={
                    "from": policy.MAIL_FROM,
                    "to": [input.email],
                    "subject": "SupportPilot 邮箱验证码"
                    if purpose == "register"
                    else "SupportPilot 重置密码",
                    "text": f"请在 SupportPilot 页面粘贴以下验证码，15分钟内有效：\n\n{token}\n\n如果不是你本人操作，请忽略。",
                },
            )
            response.raise_for_status()
    except Exception:
        raise HTTPException(503, "邮件暂时发送失败，请稍后重试") from None
    return {"status": "请检查邮箱；若符合条件，验证码将在邮件中发送"}


@router.post("/auth/register")
async def register(input: RegisterInput, request: Request):
    # Abuse checks must run before expensive password hashing.
    if not public_settings().registration_available():
        raise HTTPException(503, "注册尚未开放")
    await rate_limit("register-kdf", request.client.host if request.client else "unknown", 5, 3600)
    return await send_code(input, request, "register")


@router.post("/auth/forgot")
async def forgot(input: MailInput, request: Request):
    return await send_code(input, request, "reset")


@router.post("/auth/verify")
async def verify(input: TokenInput, request: Request):
    if not public_settings().registration_available():
        raise HTTPException(503, "注册尚未开放")
    await rate_limit("verify", request.client.host if request.client else "unknown", 20, 600)
    await business_store().consume_email_token(input.token, "register")
    return {"status": "邮箱验证成功，请登录"}


@router.post("/auth/reset")
async def reset(input: ResetInput, request: Request):
    if not public_settings().registration_available():
        raise HTTPException(503, "邮箱服务尚未开放")
    await rate_limit("reset", request.client.host if request.client else "unknown", 5, 600)
    await business_store().consume_email_token(
        input.token, "reset", await hash_password(input.password.get_secret_value())
    )
    return {"status": "密码已重置，全部旧会话已退出"}


@router.post("/auth/logout-all")
async def logout_all():
    p = require_tenant()
    async with business_store().pool.connection() as db:
        await db.execute("DELETE FROM sp_sessions WHERE user_id=%s", (p.user_id,))
    return {"status": "ok"}


class AccountPolicy(BaseModel):
    model_config = {"extra": "forbid"}
    disabled: bool | None = None
    platform_admin: bool | None = None


@router.patch("/admin/users/{user_id}")
async def account_policy(user_id: UUID, input: AccountPolicy):
    await business_store().manage_user(require_tenant(), user_id, **input.model_dump())
    return {"status": "ok"}
