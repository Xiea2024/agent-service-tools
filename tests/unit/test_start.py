"""Startup must preserve configuration/data and propagate Docker failures."""

import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location(
    "supportpilot_start", Path(__file__).parents[2] / "scripts" / "start.py"
)
start = importlib.util.module_from_spec(spec)
spec.loader.exec_module(start)


@pytest.fixture
def root(tmp_path, monkeypatch):
    (tmp_path / ".env.example").write_text(
        "USE_FAKE_MODEL=false\nDEFAULT_MODEL=\n", encoding="utf-8"
    )
    monkeypatch.setattr(start, "ROOT", tmp_path)
    monkeypatch.setattr(start, "COMPOSE", tmp_path / "compose.yaml")
    monkeypatch.setattr(start.shutil, "which", lambda _: "docker")
    return tmp_path


def test_initial_configuration_is_separate_and_never_overwritten(root):
    source = "COMPATIBLE_API_KEY=test-provider-key\nPOSTGRES_PASSWORD=old-database-password\n"
    (root / ".env").write_text(source, encoding="utf-8")
    target = root / ".env.docker"
    assert start.prepare_environment(target)
    generated = target.read_text(encoding="utf-8")
    assert "test-provider-key" in generated
    assert "old-database-password" not in generated
    assert "TENANT_MODE=true" in generated
    assert (root / ".env").read_text(encoding="utf-8") == source
    assert not start.prepare_environment(target)
    assert target.read_text(encoding="utf-8") == generated


def test_demo_does_not_copy_real_credentials(root):
    (root / ".env").write_text("COMPATIBLE_API_KEY=private-key\n", encoding="utf-8")
    target = root / ".env.docker"
    assert start.prepare_environment(target, demo=True)
    generated = target.read_text(encoding="utf-8")
    assert "private-key" not in generated
    assert "USE_FAKE_MODEL=true" in generated and "DEFAULT_MODEL=fake" in generated


def test_stop_keeps_volumes_and_uses_explicit_config(root, monkeypatch):
    target = root / ".env.docker"
    target.write_text("existing configuration", encoding="utf-8")
    run = Mock()
    monkeypatch.setattr(start.subprocess, "run", run)
    assert start.main(["--env-file", str(target), "--stop"]) == 0
    command = run.call_args.args[0]
    assert command[-1] == "down" and "--volumes" not in command and "-v" not in command
    assert run.call_args.kwargs["env"]["SUPPORTPILOT_ENV_FILE"] == str(target.resolve())
    assert target.read_text(encoding="utf-8") == "existing configuration"


def test_pull_never_builds_and_admin_password_is_not_an_argument(root, monkeypatch):
    target = root / ".env.docker"
    target.write_text("existing configuration", encoding="utf-8")
    run = Mock()
    monkeypatch.setattr(start.subprocess, "run", run)
    assert (
        start.main(["--env-file", str(target), "--pull", "--admin-email", "owner@example.com"]) == 0
    )
    commands = [call.args[0] for call in run.call_args_list]
    assert any(c[-1] == "pull" for c in commands)
    assert any("--no-build" in c and "--wait" in c for c in commands)
    assert not any("--build" in c or "--password" in c for c in commands)


def test_failed_docker_command_is_not_reported_as_success(root, monkeypatch):
    monkeypatch.setattr(
        start.subprocess, "run", Mock(side_effect=subprocess.CalledProcessError(17, "docker"))
    )
    assert start.main(["--env-file", str(root / ".env.docker"), "--demo"]) == 17


def test_stop_missing_configuration_does_not_create_credentials(root):
    target = root / "missing.env"
    with pytest.raises(SystemExit) as error:
        start.main(["--env-file", str(target), "--stop"])
    assert error.value.code == 2
    assert not target.exists()


def test_configure_only_needs_no_docker_and_starts_nothing(root, monkeypatch):
    monkeypatch.setattr(start.shutil, "which", lambda _: None)
    run = Mock()
    monkeypatch.setattr(start.subprocess, "run", run)
    target = root / ".env.docker"
    assert start.main(["--env-file", str(target), "--configure-only", "--demo"]) == 0
    assert target.exists()
    run.assert_not_called()
