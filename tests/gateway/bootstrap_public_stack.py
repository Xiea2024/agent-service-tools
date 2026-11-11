import asyncio
import json
import sys
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, "/app/src")
from tenancy.security import public_settings
from tenancy.store import password_hash, tenant_database

assert public_settings().PUBLIC_HOST == "demo.example.test", (
    "Only the synthetic test stack is allowed"
)


async def main():
    app = SimpleNamespace(state=SimpleNamespace())
    async with tenant_database(app):
        store = app.state.business
        tenant = await store.create_account(
            "admin@example.test",
            password_hash("smoke-only-password-123"),
            "Demo",
            platform_admin=True,
        )
        user = str(uuid4())
        async with store.pool.connection() as db:
            await db.execute(
                "INSERT INTO sp_users(id,email,password_hash) VALUES (%s,%s,%s)",
                (user, "member@example.test", password_hash("smoke-only-password-123")),
            )
            await db.execute("INSERT INTO sp_memberships VALUES (%s,%s,'member')", (tenant, user))
        print(json.dumps({"tenant": tenant, "member": user}))


asyncio.run(main())
