"""Initialize private deployment files and operate the public Docker stack."""

import argparse
import os
import secrets
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description="Public demo deployment; never prints credentials")
    parser.add_argument("action", choices=["init", "up", "build", "pull", "down", "logs", "check"])
    parser.add_argument("--domain", default="demo.example.com")
    args = parser.parse_args()
    if args.action == "init":
        from base64 import urlsafe_b64encode

        files = [ROOT / ".env.public", ROOT / ".env.gateway"]
        if any(p.exists() for p in files):
            parser.error("Private configuration already exists; refusing to overwrite")
        password, admin_password, db_password = (secrets.token_hex(24) for _ in range(3))
        master, salt = "sk-" + secrets.token_hex(32), secrets.token_hex(32)
        encryption = urlsafe_b64encode(secrets.token_bytes(32)).decode()
        contents = [
            f"""POSTGRES_USER=sp_app
POSTGRES_DB=supportpilot
POSTGRES_PASSWORD={password}
POSTGRES_ADMIN_PASSWORD={admin_password}
GATEWAY_DB_PASSWORD={db_password}
GATEWAY_MASTER_KEY={master}
GATEWAY_ENCRYPTION_KEY={encryption}
PUBLIC_HOST={args.domain}
PUBLIC_ORIGIN=https://{args.domain}
REGISTRATION_ENABLED=false
DEMO_TENANT_ID=
SECURITY_REDIS_URL=redis://redis:6379/0
RESEND_API_KEY=
MAIL_FROM=
TURNSTILE_SITE_KEY=
TURNSTILE_SECRET_KEY=
EMBEDDING_API_KEY=gateway-context-only
COMPATIBLE_API_KEY=gateway-context-only
DEFAULT_MODEL=openai-compatible
USE_FAKE_MODEL=false
LANGCHAIN_TRACING_V2=false
LANGFUSE_TRACING=false
PAID_CALLS_ENABLED=true
SUPPORTPILOT_IMAGE_PREFIX=ghcr.io/xiea2024/agent-service-tools
IMAGE_TAG=latest
LITELLM_IMAGE=ghcr.io/berriai/litellm:v1.108.4
""",
            f"""LITELLM_MASTER_KEY={master}
LITELLM_SALT_KEY={salt}
UPSTREAM_CHAT_MODEL=openai/REPLACE_WITH_MODEL
UPSTREAM_CHAT_BASE=https://REPLACE_WITH_PROVIDER/v1
UPSTREAM_CHAT_KEY=REPLACE_WITH_PROVIDER_KEY
UPSTREAM_EMBEDDING_MODEL=openai/REPLACE_WITH_EMBEDDING_MODEL
UPSTREAM_EMBEDDING_BASE=https://REPLACE_WITH_PROVIDER/v1
UPSTREAM_EMBEDDING_KEY=REPLACE_WITH_PROVIDER_KEY
""",
        ]
        for path, content in zip(files, contents, strict=True):
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                output.write(content)
            if os.name != "nt":
                path.chmod(0o600)
        (ROOT / "docker/tls").mkdir(exist_ok=True)
        print(
            "Created .env.public and .env.gateway. Configure providers, mail, Turnstile and TLS before startup."
        )
        return
    command = [
        "docker",
        "compose",
        "--env-file",
        str(ROOT / ".env.public"),
        "-f",
        str(ROOT / "docker/compose.public.yaml"),
    ]
    if args.action == "up":
        for name in ("fullchain.pem", "privkey.pem"):
            if not (ROOT / "docker/tls" / name).is_file():
                parser.error(f"Missing TLS certificate file: docker/tls/{name}")
    actions = {
        "up": ["up", "-d", "--no-build", "--wait", "--wait-timeout", "300"],
        "build": ["build"],
        "pull": ["pull"],
        "down": ["down"],
        "logs": ["logs", "--tail", "100", "-f"],
        "check": ["config", "--quiet"],
    }
    subprocess.run([*command, *actions[args.action]], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
