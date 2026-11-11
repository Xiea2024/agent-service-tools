"""Server-only LiteLLM keys; callers cannot choose the charged identity."""

from contextvars import ContextVar

import httpx
from fastapi import HTTPException

from tenancy.security import public_settings

gateway_key: ContextVar[str | None] = ContextVar("gateway_key", default=None)


async def user_gateway_key(store, principal):
    from cryptography.fernet import Fernet

    policy = public_settings()
    assert policy.GATEWAY_ENCRYPTION_KEY and policy.GATEWAY_MASTER_KEY
    cipher = Fernet(policy.GATEWAY_ENCRYPTION_KEY.get_secret_value().encode())
    async with store.pool.connection() as db, db.transaction():
        await db.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            ("gateway:" + principal.user_id,),
        )
        row = await (
            await db.execute(
                "SELECT encrypted_key FROM sp_gateway_keys WHERE user_id=%s",
                (principal.user_id,),
            )
        ).fetchone()
        if row:
            return cipher.decrypt(row["encrypted_key"].encode()).decode()
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    policy.GATEWAY_URL + "/key/generate",
                    headers={
                        "Authorization": "Bearer " + policy.GATEWAY_MASTER_KEY.get_secret_value()
                    },
                    json={
                        "models": [policy.DEMO_CHAT_MODEL, policy.DEMO_EMBEDDING_MODEL],
                        "max_budget": policy.DEMO_KEY_DAILY_BUDGET,
                        "budget_duration": "24h",
                        "rpm_limit": 12,
                        "tpm_limit": 20000,
                        "max_parallel_requests": 1,
                        "metadata": {"supportpilot_user_id": principal.user_id},
                    },
                )
                response.raise_for_status()
                key = response.json()["key"]
        except Exception:
            raise HTTPException(503, "模型网关暂不可用") from None
        await db.execute(
            "INSERT INTO sp_gateway_keys VALUES (%s,%s)",
            (principal.user_id, cipher.encrypt(key.encode()).decode()),
        )
        return key


def public_chat_model():
    from langchain_openai import ChatOpenAI

    policy = public_settings()
    key = gateway_key.get()
    if not key or not policy.PAID_CALLS_ENABLED:
        raise HTTPException(503, "模型服务暂停")
    return ChatOpenAI(
        model=policy.DEMO_CHAT_MODEL,
        base_url=policy.GATEWAY_URL + "/v1",
        api_key=key,
        max_tokens=2000,
        max_retries=0,
        timeout=60,
        streaming=True,
    )
