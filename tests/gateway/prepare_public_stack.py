import datetime
import shutil
import subprocess
import sys
from pathlib import Path

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

root = Path.cwd()
target = root / ".tmp/public-stack"
(target / "scripts").mkdir(parents=True, exist_ok=True)
shutil.copy(root / "scripts/public_deploy.py", target / "scripts/public_deploy.py")
shutil.copytree(
    root / "docker",
    target / "docker",
    dirs_exist_ok=True,
    ignore=shutil.ignore_patterns("tls", "secrets"),
)
subprocess.run(
    [
        sys.executable,
        str(target / "scripts/public_deploy.py"),
        "init",
        "--domain",
        "demo.example.test",
    ],
    check=True,
)
p = target / ".env.public"
s = (
    p.read_text()
    .replace(
        "SUPPORTPILOT_IMAGE_PREFIX=ghcr.io/xiea2024/agent-service-tools",
        "SUPPORTPILOT_IMAGE_PREFIX=supportpilot",
    )
    .replace("IMAGE_TAG=latest", "IMAGE_TAG=security-check")
)
p.write_text(s, encoding="utf-8")
p = target / ".env.gateway"
s = (
    p.read_text()
    .replace("openai/REPLACE_WITH_MODEL", "openai/mock-chat")
    .replace("openai/REPLACE_WITH_EMBEDDING_MODEL", "openai/mock-embedding")
    .replace("https://REPLACE_WITH_PROVIDER/v1", "http://mock-provider:8000/v1")
)
p.write_text(s, encoding="utf-8")
p = target / "docker/compose.public.yaml"
data = yaml.safe_load(p.read_text())
data["services"]["nginx"]["ports"] = ["127.0.0.1:11080:80", "127.0.0.1:11443:443"]
data["services"]["mock-provider"] = {
    "image": "python:3.13-slim",
    "command": ["python", "/test/mock_provider.py"],
    "volumes": [str(root / "tests/gateway") + ":/test:ro"],
}
p.write_text(yaml.safe_dump(data), encoding="utf-8")
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "demo.example.test")])
now = datetime.datetime.now(datetime.UTC)
cert = (
    x509.CertificateBuilder()
    .subject_name(name)
    .issuer_name(name)
    .public_key(key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(now - datetime.timedelta(minutes=1))
    .not_valid_after(now + datetime.timedelta(days=1))
    .add_extension(x509.SubjectAlternativeName([x509.DNSName("demo.example.test")]), critical=False)
    .sign(key, hashes.SHA256())
)
(target / "docker/tls/privkey.pem").write_bytes(
    key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
)
(target / "docker/tls/fullchain.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
print("Prepared isolated public smoke stack with synthetic credentials and certificate")
