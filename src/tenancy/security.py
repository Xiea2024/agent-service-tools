"""Public deployment policy and shared, fail-closed abuse controls."""

import hashlib
from functools import cache
from typing import Any

from fastapi import HTTPException
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class PublicSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)
    PUBLIC_DEMO: bool = False
    REGISTRATION_ENABLED: bool = False
    PUBLIC_ORIGIN: str = ""
    PUBLIC_HOST: str = ""
    SECURITY_REDIS_URL: SecretStr = SecretStr("redis://redis:6379/0")
    RESEND_API_KEY: SecretStr | None = None
    MAIL_FROM: str = ""
    TURNSTILE_SITE_KEY: str = ""
    TURNSTILE_SECRET_KEY: SecretStr | None = None
    GATEWAY_URL: str = "http://litellm:4000"
    GATEWAY_MASTER_KEY: SecretStr | None = None
    GATEWAY_ENCRYPTION_KEY: SecretStr | None = None
    DEMO_TENANT_ID: str = ""
    DEMO_CHAT_MODEL: str = "demo-chat"
    DEMO_EMBEDDING_MODEL: str = "demo-embedding"
    DEMO_EMBEDDING_DIMENSION: int = Field(default=1024, ge=1, le=8192)
    DEMO_DAILY_QUESTIONS: int = Field(default=10, ge=1)
    DEMO_GLOBAL_DAILY_QUESTIONS: int = Field(default=60, ge=1)
    DEMO_GLOBAL_CONCURRENCY: int = Field(default=3, ge=1, le=20)
    DEMO_DAILY_ACTIVATIONS: int = Field(default=30, ge=1)
    DEMO_DAILY_EMAILS: int = Field(default=80, ge=1)
    DEMO_KEY_DAILY_BUDGET: float = Field(default=0.1, gt=0)
    PAID_CALLS_ENABLED: bool = True

    def registration_available(self):
        return bool(
            self.PUBLIC_DEMO
            and self.REGISTRATION_ENABLED
            and self.RESEND_API_KEY
            and self.MAIL_FROM
            and self.TURNSTILE_SECRET_KEY
            and self.TURNSTILE_SITE_KEY
            and self.DEMO_TENANT_ID
        )

    def validate_production(self, settings):
        if not self.PUBLIC_DEMO:
            return
        from urllib.parse import urlsplit

        origin = urlsplit(self.PUBLIC_ORIGIN)
        if (
            not settings.TENANT_MODE
            or not settings.TENANT_COOKIE_SECURE
            or origin.scheme != "https"
            or origin.netloc != self.PUBLIC_HOST
            or origin.path
            or origin.query
            or origin.fragment
            or not self.GATEWAY_MASTER_KEY
            or not self.GATEWAY_ENCRYPTION_KEY
        ):
            raise ValueError(
                "Public demo requires tenant authentication, HTTPS, host and gateway secrets"
            )
        from uuid import UUID

        from cryptography.fernet import Fernet

        Fernet(self.GATEWAY_ENCRYPTION_KEY.get_secret_value().encode())
        if self.DEMO_TENANT_ID:
            UUID(self.DEMO_TENANT_ID)
        if self.GATEWAY_URL != "http://litellm:4000":
            raise ValueError("Public demo must use the internal LiteLLM service")
        if settings.USE_FAKE_MODEL:
            raise ValueError("Public demo requires the model gateway")


@cache
def public_settings():
    return PublicSettings()


@cache
def security_redis() -> Any:
    from redis.asyncio import Redis

    return Redis.from_url(public_settings().SECURITY_REDIS_URL.get_secret_value())


async def rate_limit(bucket: str, identity: str, limit: int, seconds: int):
    """Increment and expiry are atomic across processes; never store raw emails/IPs."""
    key = "sp:limit:" + bucket + ":" + hashlib.sha256(identity.encode()).hexdigest()
    try:
        count = await security_redis().eval(
            "local n=redis.call('INCR',KEYS[1]); if n==1 then "
            "redis.call('EXPIRE',KEYS[1],ARGV[1]) end; return n",
            1,
            key,
            seconds,
        )
    except Exception:
        raise HTTPException(503, "安全服务暂不可用，请稍后重试") from None
    if count > limit:
        raise HTTPException(429, "操作过于频繁或今日额度已用完，请稍后重试")


async def verify_human(token: str):
    import httpx

    policy = public_settings()
    if not policy.TURNSTILE_SECRET_KEY:
        raise HTTPException(503, "注册验证尚未配置")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={"secret": policy.TURNSTILE_SECRET_KEY.get_secret_value(), "response": token},
            )
            response.raise_for_status()
            result = response.json()
    except Exception:
        raise HTTPException(503, "人机验证暂不可用") from None
    if not result.get("success") or result.get("hostname") != policy.PUBLIC_HOST:
        raise HTTPException(400, "请重新完成人机验证")
