"""Business persistence; independent of LangGraph's checkpoint schema.

Every resource lookup includes both tenant and owner. Client thread IDs never address
checkpoints directly. Schema migrations run transactionally under an advisory lock.
"""

import asyncio
import hashlib
import hmac
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool


def password_hash(password: str) -> str:
    if not 10 <= len(password) <= 256:
        raise ValueError("密码长度必须为 10–256 个字符")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, salt, expected = encoded.split("$")
        if algorithm != "scrypt" or len(password) > 256:
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Principal:
    user_id: str
    tenant_id: str
    role: str

    @property
    def memory_user_id(self) -> str:
        return f"{self.tenant_id}:{self.user_id}"


class BusinessStore:
    def __init__(self, pool):
        self.pool = pool

    async def migrate(self):
        async with self.pool.connection() as db, db.transaction():
            await db.execute("SELECT pg_advisory_xact_lock(731294810)")
            await db.execute(
                "CREATE TABLE IF NOT EXISTS sp_schema_versions "
                "(version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            rows = await (await db.execute("SELECT version FROM sp_schema_versions")).fetchall()
            applied = {r["version"] for r in rows}
            for path in sorted((Path(__file__).parent / "migrations").glob("*.sql")):
                version = int(path.name.split("_")[0])
                if version not in applied:
                    await db.execute(path.read_text(encoding="utf-8"), prepare=False)
                    await db.execute(
                        "INSERT INTO sp_schema_versions(version) VALUES (%s)", (version,)
                    )

    async def create_account(self, email: str, encoded: str, team: str):
        user, tenant = str(uuid4()), str(uuid4())
        async with self.pool.connection() as db, db.transaction():
            await db.execute(
                "INSERT INTO sp_users(id,email,password_hash) VALUES (%s,%s,%s)",
                (user, email.strip().lower(), encoded),
            )
            await db.execute("INSERT INTO sp_tenants(id,name) VALUES (%s,%s)", (tenant, team))
            await db.execute("INSERT INTO sp_memberships VALUES (%s,%s,'admin')", (tenant, user))
        return tenant

    async def login_user(self, email):
        async with self.pool.connection() as db:
            return await (
                await db.execute("SELECT * FROM sp_users WHERE email=%s", (email.strip().lower(),))
            ).fetchone()

    async def create_session(self, user_id):
        token = secrets.token_urlsafe(32)
        async with self.pool.connection() as db:
            await db.execute("DELETE FROM sp_sessions WHERE expires_at <= now()")
            await db.execute(
                "INSERT INTO sp_sessions VALUES (%s,%s,now()+interval '12 hours')",
                (token_hash(token), user_id),
            )
        return token

    async def revoke_session(self, token):
        async with self.pool.connection() as db:
            await db.execute("DELETE FROM sp_sessions WHERE token_hash=%s", (token_hash(token),))

    async def authenticate(self, token, tenant_id=None):
        async with self.pool.connection() as db:
            user = await (
                await db.execute(
                    "SELECT u.id,u.email FROM sp_sessions s JOIN sp_users u ON u.id=s.user_id "
                    "WHERE s.token_hash=%s AND s.expires_at > now()",
                    (token_hash(token),),
                )
            ).fetchone()
            if not user:
                raise HTTPException(401, "请先登录")
            memberships = await (
                await db.execute(
                    "SELECT t.id::text,t.name,m.role FROM sp_memberships m "
                    "JOIN sp_tenants t ON t.id=m.tenant_id WHERE m.user_id=%s ORDER BY t.created_at,t.id",
                    (user["id"],),
                )
            ).fetchall()
        selected = (
            next((m for m in memberships if m["id"] == tenant_id), None)
            if tenant_id
            else next(iter(memberships), None)
        )
        if not selected:
            raise HTTPException(403, "没有该团队的访问权限")
        return Principal(str(user["id"]), selected["id"], selected["role"]), {
            "user_id": str(user["id"]),
            "email": user["email"],
            "tenants": memberships,
            "tenant_id": selected["id"],
        }

    async def create_team(self, principal, name):
        tenant = str(uuid4())
        async with self.pool.connection() as db, db.transaction():
            await db.execute("INSERT INTO sp_tenants(id,name) VALUES (%s,%s)", (tenant, name))
            await db.execute(
                "INSERT INTO sp_memberships VALUES (%s,%s,'admin')", (tenant, principal.user_id)
            )
        return {"id": tenant, "name": name, "role": "admin"}

    async def members(self, p):
        async with self.pool.connection() as db:
            return await (
                await db.execute(
                    "SELECT u.id::text,u.email,m.role FROM sp_memberships m JOIN sp_users u "
                    "ON u.id=m.user_id WHERE m.tenant_id=%s ORDER BY u.email",
                    (p.tenant_id,),
                )
            ).fetchall()

    async def add_member(self, p, email, encoded, role):
        if p.role != "admin":
            raise HTTPException(403, "需要团队管理员权限")
        async with self.pool.connection() as db, db.transaction():
            if encoded:
                await db.execute(
                    "INSERT INTO sp_users(id,email,password_hash) VALUES (%s,%s,%s) "
                    "ON CONFLICT (email) DO NOTHING",
                    (str(uuid4()), email, encoded),
                )
            user = await (
                await db.execute("SELECT id FROM sp_users WHERE email=%s", (email,))
            ).fetchone()
            if not user:
                raise HTTPException(422, "新成员需要设置初始密码")
            await db.execute(
                "INSERT INTO sp_memberships VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (p.tenant_id, user["id"], role),
            )

    async def thread(self, p, thread_id, agent_id, *, title=None):
        if not thread_id or len(thread_id) > 128:
            raise HTTPException(422, "无效的会话编号")
        async with self.pool.connection() as db, db.transaction():
            if title is not None:
                await db.execute(
                    "INSERT INTO sp_threads(id,checkpoint_id,tenant_id,user_id,agent_id,title) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (thread_id, str(uuid4()), p.tenant_id, p.user_id, agent_id, title[:60]),
                )
            row = await (
                await db.execute(
                    "SELECT checkpoint_id::text FROM sp_threads WHERE id=%s AND tenant_id=%s "
                    "AND user_id=%s AND agent_id=%s",
                    (thread_id, p.tenant_id, p.user_id, agent_id),
                )
            ).fetchone()
            if not row:
                raise HTTPException(404, "会话不存在")
            return row["checkpoint_id"]

    async def threads(self, p, agent_id, limit):
        async with self.pool.connection() as db:
            return await (
                await db.execute(
                    "SELECT id AS thread_id,agent_id,title,updated_at FROM sp_threads "
                    "WHERE tenant_id=%s AND user_id=%s AND agent_id=%s ORDER BY updated_at DESC LIMIT %s",
                    (p.tenant_id, p.user_id, agent_id, limit),
                )
            ).fetchall()

    async def start_run(self, p, run_id, thread_id, question):
        try:
            async with self.pool.connection() as db, db.transaction():
                await db.execute(
                    "INSERT INTO sp_runs(id,thread_id,tenant_id,user_id,status,question) "
                    "VALUES (%s,%s,%s,%s,'running',%s)",
                    (str(run_id), thread_id, p.tenant_id, p.user_id, question),
                )
                await db.execute(
                    "UPDATE sp_threads SET updated_at=now() WHERE id=%s AND tenant_id=%s AND user_id=%s",
                    (thread_id, p.tenant_id, p.user_id),
                )
        except UniqueViolation:
            raise HTTPException(409, "该会话仍有执行中的任务，请等待完成或检查运行状态") from None

    async def finish_run(self, p, run_id, status, answer="", evidence=None):
        async with self.pool.connection() as db, db.transaction():
            row = await (
                await db.execute(
                    "UPDATE sp_runs SET status=%s,finished_at=now() WHERE id=%s AND tenant_id=%s "
                    "AND user_id=%s AND status='running' RETURNING id",
                    (status, str(run_id), p.tenant_id, p.user_id),
                )
            ).fetchone()
            if row and status == "succeeded":
                await db.execute(
                    "INSERT INTO sp_answers(run_id,tenant_id,user_id,answer,evidence) VALUES (%s,%s,%s,%s,%s)",
                    (str(run_id), p.tenant_id, p.user_id, answer, Jsonb(evidence or [])),
                )

    async def run(self, p, run_id):
        async with self.pool.connection() as db:
            row = await (
                await db.execute(
                    "SELECT r.id::text AS run_id,r.thread_id,r.status,r.question,r.created_at,r.finished_at,"
                    "a.answer,a.evidence,f.helpful,f.comment FROM sp_runs r "
                    "LEFT JOIN sp_answers a ON a.run_id=r.id LEFT JOIN sp_feedback f ON f.run_id=r.id "
                    "WHERE r.id=%s AND r.tenant_id=%s AND r.user_id=%s",
                    (str(run_id), p.tenant_id, p.user_id),
                )
            ).fetchone()
        if not row:
            raise HTTPException(404, "运行记录不存在")
        return row

    async def feedback(self, p, run_id, helpful, comment):
        async with self.pool.connection() as db:
            row = await (
                await db.execute(
                    "INSERT INTO sp_feedback(run_id,tenant_id,user_id,helpful,comment) "
                    "SELECT run_id,tenant_id,user_id,%s,%s FROM sp_answers "
                    "WHERE run_id=%s AND tenant_id=%s AND user_id=%s "
                    "ON CONFLICT DO NOTHING RETURNING run_id",
                    (helpful, comment, str(run_id), p.tenant_id, p.user_id),
                )
            ).fetchone()
        if not row:
            existing = await self.run(p, run_id)
            if existing["answer"] is None:
                raise HTTPException(409, "该运行尚无可评价的回答")
            raise HTTPException(409, "该回答已经评价")

    async def documents(self, p):
        async with self.pool.connection() as db:
            return await (
                await db.execute(
                    "SELECT document_id,title,source_url,chunk_count,updated_at FROM sp_documents "
                    "WHERE tenant_id=%s ORDER BY updated_at DESC",
                    (p.tenant_id,),
                )
            ).fetchall()

    async def import_document(self, p, data, upload):
        fingerprint = hashlib.sha256(data.batch.model_dump_json().encode()).hexdigest()
        # Cross-store writes are retryable, not atomic. Serialize imports of the
        # same tenant/document across service instances before touching Qdrant.
        async with self.pool.connection() as db, db.transaction():
            await db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"{p.tenant_id}:{data.batch.document.document_id}",),
            )
            existing = await (
                await db.execute(
                    "SELECT fingerprint FROM sp_documents WHERE tenant_id=%s AND document_id=%s",
                    (p.tenant_id, data.batch.document.document_id),
                )
            ).fetchone()
            if existing and existing["fingerprint"] != fingerprint:
                raise HTTPException(409, "文档更新同步尚未实现，请使用新的文档编号")
            count = await asyncio.to_thread(upload)
            await db.execute(
                "INSERT INTO sp_documents(tenant_id,document_id,title,source_url,fingerprint,chunk_count) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (tenant_id,document_id) DO UPDATE "
                "SET updated_at=now() WHERE sp_documents.fingerprint=EXCLUDED.fingerprint",
                (
                    p.tenant_id,
                    data.batch.document.document_id,
                    data.batch.document.title,
                    data.batch.document.source_url,
                    fingerprint,
                    count,
                ),
            )
            return count


@asynccontextmanager
async def tenant_database(app):
    from core import settings
    from memory.postgres import get_postgres_connection_string, validate_postgres_config

    if not settings.TENANT_MODE:
        yield
        return
    if settings.DATABASE_TYPE != "postgres":
        raise ValueError("TENANT_MODE=true 要求 DATABASE_TYPE=postgres")
    validate_postgres_config()
    async with AsyncConnectionPool(
        get_postgres_connection_string(),
        min_size=1,
        max_size=5,
        kwargs={"row_factory": dict_row},
        open=False,
    ) as pool:
        await pool.wait(timeout=15)
        store = BusinessStore(pool)
        await store.migrate()
        app.state.business = store
        yield
