"""长期记忆接入图的方式：写入、召回、冲突与关闭条件。"""

from unittest.mock import Mock

import pytest
from langchain_core.messages import HumanMessage

from agents.dev_issue_agent import _system_prompt, prepare_turn
from core import settings
from memory_facts import ENV_OS, ENV_VERSION, fact_store
from observability import stages

CONFIG = {"configurable": {"user_id": "u-1"}, "metadata": {"run_id": "mem-run"}}


def turn(text: str, config: dict = CONFIG):
    return prepare_turn({"messages": [HumanMessage(content=text)]}, config)


@pytest.fixture(autouse=True)
def clean():
    stages.clear_stages()
    yield
    stages.clear_stages()


def test_first_turn_writes_and_recalls_nothing_yet():
    result = turn("Windows 上 Python 3.12 启动失败")
    # 写入的事实会在本轮立即回读，因此同一轮就能拿到。
    assert ENV_OS in result["memory_block"]
    assert ENV_VERSION in result["memory_block"]
    records = [r for r in stages.stages_for("mem-run") if r["stage"] == "memory"]
    assert records[0]["decisions"]["written"] == 2
    assert records[0]["outcome"] == "success"


def test_next_turn_recalls_written_facts():
    turn("我在 Windows 上开发，Python 3.12")
    result = turn("现在启动又失败了")
    assert "- env.os: Windows" in result["memory_block"]
    assert "- env.version: Python 3.12" in result["memory_block"]
    records = [r for r in stages.stages_for("mem-run") if r["stage"] == "memory"]
    assert records[-1]["decisions"]["hit"] == 2
    assert records[-1]["decisions"]["written"] == 0


def test_conflict_is_reported_and_old_value_kept():
    turn("我用的 Windows")
    result = turn("不对，我用的其实是 Linux")
    assert "记录为 Windows，本次说的是 Linux" in result["memory_block"]
    assert "先向用户确认" in result["memory_block"]
    assert [fact.value for fact in fact_store().recall("u-1") if fact.key == ENV_OS] == ["Windows"]


def test_missing_user_id_skips_memory():
    result = prepare_turn({"messages": [HumanMessage(content="Windows 上启动失败")]}, {})
    assert result["memory_block"] == ""
    records = [r for r in stages.stages_for("_unbound") if r["stage"] == "memory"]
    assert records[0]["outcome"] == "skipped"


@pytest.mark.parametrize("flag", ["TENANT_MODE"])
def test_tenant_mode_disables_memory(monkeypatch, flag):
    monkeypatch.setattr(settings, flag, True)
    result = turn("Windows 上 Python 3.12 启动失败")
    assert result["memory_block"] == ""
    assert [r for r in stages.stages_for("mem-run") if r["stage"] == "memory"] == []


def test_memory_failure_does_not_break_the_turn(monkeypatch):
    broken = Mock()
    broken.remember.side_effect = RuntimeError("disk full")
    monkeypatch.setattr("agents.dev_issue_agent.fact_store", lambda: broken)
    result = turn("Windows 上启动失败")
    assert result["memory_block"] == ""
    records = [r for r in stages.stages_for("mem-run") if r["stage"] == "memory"]
    assert records[0]["outcome"] == "error"


def test_system_prompt_carries_facts_and_the_instruction():
    block = "- env.os: Windows"
    prompt = _system_prompt("BASE", {"memory_block": block})
    assert prompt.startswith("BASE")
    assert block in prompt
    assert "不要重复追问" in prompt


def test_system_prompt_without_memory_is_unchanged():
    assert _system_prompt("BASE", {}) == "BASE"
    assert _system_prompt("BASE", {"memory_block": ""}) == "BASE"
    assert _system_prompt("BASE", {}, "EXTRA").endswith("EXTRA")
