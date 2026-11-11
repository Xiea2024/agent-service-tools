"""One-command Docker startup; only Python's standard library is required.

Uses a separate .env.docker and named volumes. Existing .env and data are never
overwritten. Stop removes containers/networks while retaining all data volumes.
"""

import argparse
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker" / "compose.app.yaml"


def prepare_environment(path: Path, *, demo: bool = False) -> bool:
    if path.exists():
        return False
    source = ROOT / ".env"
    template = source if source.exists() and not demo else ROOT / ".env.example"
    content = template.read_text(encoding="utf-8-sig")
    # Provider settings are copied, but credentials for the isolated Docker stack
    # are generated once. Subsequent starts always reuse the existing file.
    values = {
        "TENANT_MODE": "true",
        "DATABASE_TYPE": "postgres",
        "POSTGRES_USER": "supportpilot",
        "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "POSTGRES_DB": "supportpilot",
        "POSTGRES_HOST": "postgres",
        "POSTGRES_PORT": "5432",
        "TENANT_COOKIE_SECURE": "false",
        "QDRANT_URL": "http://qdrant:6333",
        "WEB_PORT": "8088",
    }
    if demo or not source.exists():
        values.update(
            {
                "USE_FAKE_MODEL": "true",
                "DEFAULT_MODEL": "fake",
                "LANGFUSE_TRACING": "false",
                "LANGCHAIN_TRACING_V2": "false",
            }
        )
    for key, value in values.items():
        pattern = rf"(?m)^{re.escape(key)}\s*=.*$"
        if re.search(pattern, content):
            content = re.sub(pattern, lambda _: f"{key}={value}", content)
        else:
            content += f"\n{key}={value}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as output:
        output.write(content.rstrip() + "\n")
    if os.name != "nt":
        path.chmod(0o600)
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="启动 SupportPilot 团队工作台、PostgreSQL 和 Qdrant"
    )
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.docker")
    parser.add_argument(
        "--configure-only", action="store_true", help="只初始化配置，不构建或启动容器"
    )
    parser.add_argument(
        "--demo", action="store_true", help="首次创建配置时使用假模型，不调用外部模型"
    )
    parser.add_argument(
        "--pull", action="store_true", help="拉取配置指定的 GHCR 镜像，不在本机构建"
    )
    parser.add_argument("--stop", action="store_true", help="停止整个栈，保留数据卷")
    parser.add_argument("--logs", action="store_true", help="查看服务日志")
    parser.add_argument("--admin-email", help="启动后交互创建管理员；密码不会进入命令行")
    parser.add_argument("--team", default="研发团队", help="新管理员的团队名称")
    args = parser.parse_args(argv)
    if sum((args.stop, args.logs, args.pull, args.configure_only)) > 1:
        parser.error("--stop、--logs、--pull、--configure-only 不能同时使用")
    if (args.stop or args.logs) and (args.admin_email or args.demo):
        parser.error("停止/日志模式不能创建管理员或演示配置")
    if args.configure_only and args.admin_email:
        parser.error("--configure-only 不会启动服务，不能同时创建管理员")
    env_file = args.env_file.expanduser().resolve()
    if not args.configure_only and not shutil.which("docker"):
        parser.error("未找到 Docker，请安装并启动 Docker Desktop / Docker Engine")
    if not env_file.exists() and (args.stop or args.logs or args.pull):
        parser.error(f"配置文件不存在：{env_file}；请先启动一次或准备 GHCR 配置")
    created = prepare_environment(env_file, demo=args.demo)
    if created:
        print(f"已创建独立配置：{env_file}；已有 .env 保持不变。", flush=True)
    elif args.demo:
        print("配置已存在，--demo 不会覆盖已有模型设置。", flush=True)
    if args.configure_only:
        print(
            "配置已就绪。可编辑模型配置、SUPPORTPILOT_IMAGE_PREFIX 和 IMAGE_TAG，再使用 --pull 启动。"
        )
        return 0
    env = {**os.environ, "SUPPORTPILOT_ENV_FILE": str(env_file)}
    command = ["docker", "compose", "--env-file", str(env_file), "-f", str(COMPOSE)]

    def run(*tail, capture_output=False):
        return subprocess.run(
            [*command, *tail],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=capture_output,
            text=True,
        )

    try:
        if args.stop:
            run("down")
        elif args.logs:
            run("logs", "--follow", "--tail", "100")
        else:
            run("config", "--quiet")
            if args.pull:
                run("pull")
                run("up", "--detach", "--no-build", "--wait", "--wait-timeout", "180")
            else:
                run("up", "--detach", "--build", "--wait", "--wait-timeout", "180")
            if args.admin_email:
                run(
                    "exec",
                    "backend",
                    "python",
                    "scripts/create_tenant_admin.py",
                    "--email",
                    args.admin_email,
                    "--team",
                    args.team,
                )
            run("ps")
            address = run("port", "web", "80", capture_output=True).stdout.strip()
            print(f"工作台已启动：http://{address}/supportpilot")
            if not args.admin_email:
                print("首次使用请创建管理员：python scripts/start.py --admin-email 你的邮箱")
            print("停止并保留数据：python scripts/start.py --stop")
        return 0
    except subprocess.CalledProcessError as exc:
        print(
            f"Docker 命令失败（退出码 {exc.returncode}），请检查 Docker 状态或使用 --logs 查看日志。",
            file=sys.stderr,
        )
        return exc.returncode
    except KeyboardInterrupt:
        print("\n已结束当前命令；已启动的容器继续运行。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
