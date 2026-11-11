"""Create the first account and its team without exposing public registration."""

import argparse
import asyncio
import getpass
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from psycopg.errors import UniqueViolation  # noqa: E402

from tenancy.store import password_hash, tenant_database  # noqa: E402


async def create(email, team, encoded):
    app = SimpleNamespace(state=SimpleNamespace())
    async with tenant_database(app):
        if not hasattr(app.state, "business"):
            raise ValueError("请先设置 TENANT_MODE=true 和 PostgreSQL 连接配置")
        from tenancy.security import public_settings

        return await app.state.business.create_account(
            email, encoded, team, platform_admin=public_settings().PUBLIC_DEMO
        )


def main():
    parser = argparse.ArgumentParser(
        description="创建 SupportPilot 团队管理员；密码交互输入，不进入命令历史"
    )
    parser.add_argument("--email", required=True)
    parser.add_argument("--team", required=True)
    args = parser.parse_args()
    email, team = args.email.strip().lower(), args.team.strip()
    if "@" not in email or len(email) > 254 or not 1 <= len(team) <= 80:
        parser.error("请提供有效邮箱和 1–80 字符的团队名称")
    try:
        password = getpass.getpass("初始密码（至少 10 字符）: ")
        if password != getpass.getpass("再次输入密码: "):
            parser.error("两次密码不一致")
        encoded = password_hash(password)
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        tenant = asyncio.run(create(email, team, encoded))
        print(f"管理员与团队已创建。团队 ID：{tenant}")
    except UniqueViolation:
        parser.exit(1, "该邮箱已存在；请使用现有账号登录后创建团队。\n")
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
