"""长期记忆的事实抽取、冲突处理与过期策略。"""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from memory_facts import (
    ENV_OS,
    ENV_VERSION,
    ISSUE_COMPONENT,
    FactStore,
    extract_facts,
    render_block,
)


@pytest.fixture
def store(tmp_path):
    return FactStore(tmp_path / "facts.sqlite3")


def test_extracts_only_stated_facts():
    text = "Windows 上 Python 3.12 项目启动失败，POST /api/orders 返回 500"
    assert dict(extract_facts(text)) == {
        ENV_OS: "Windows",
        ENV_VERSION: "Python 3.12",
        ISSUE_COMPONENT: "POST /api/orders",
    }


def test_chinese_wording_is_understood():
    facts = dict(extract_facts("麒麟系统上接口 /internal/health 超时"))
    assert facts[ENV_OS] == "麒麟"
    assert facts[ISSUE_COMPONENT] == "/internal/health"


def test_plain_question_extracts_nothing():
    assert extract_facts("我们的接口突然报错了，帮我看看") == []
    assert extract_facts("   ") == []


def test_version_does_not_confuse_node_and_python():
    assert dict(extract_facts("Node 20 上跑不起来"))[ENV_VERSION] == "Node 20"
    assert dict(extract_facts("JDK 17 报 NoClassDefFoundError"))[ENV_VERSION] == "JDK 17"


def test_remember_and_recall_roundtrip(store):
    written, conflicts = store.remember("u1", [(ENV_OS, "Windows")], "run-1")
    assert written == [ENV_OS]
    assert conflicts == []
    assert [(fact.key, fact.value) for fact in store.recall("u1")] == [(ENV_OS, "Windows")]


def test_same_value_is_not_duplicated(store):
    store.remember("u1", [(ENV_OS, "Windows")])
    written, conflicts = store.remember("u1", [(ENV_OS, "Windows")])
    assert written == []
    assert conflicts == []
    assert len(store.recall("u1")) == 1


def test_conflict_keeps_the_old_value(store):
    store.remember("u1", [(ENV_OS, "Windows")])
    written, conflicts = store.remember("u1", [(ENV_OS, "Linux")])
    assert written == []
    assert len(conflicts) == 1
    assert conflicts[0].remembered == "Windows"
    assert conflicts[0].stated == "Linux"
    # 旧值不能被静默覆盖。
    assert [(fact.key, fact.value) for fact in store.recall("u1")] == [(ENV_OS, "Windows")]


def test_facts_are_isolated_per_user(store):
    store.remember("u1", [(ENV_OS, "Windows")])
    store.remember("u2", [(ENV_OS, "Linux")])
    assert store.recall("u1")[0].value == "Windows"
    assert store.recall("u2")[0].value == "Linux"


def test_unknown_keys_are_rejected(store):
    written, _ = store.remember("u1", [("user.password", "secret")])
    assert written == []
    assert store.recall("u1") == []


def test_recall_honours_limit(store):
    store.remember("u1", [(ENV_OS, "Windows"), (ENV_VERSION, "Python 3.12")])
    assert len(store.recall("u1")) == 2
    assert len(store.recall("u1", limit=1)) == 1


def test_expired_facts_are_not_recalled_and_get_replaced(store):
    store.remember("u1", [(ENV_OS, "Windows")])
    old = (datetime.now(UTC) - timedelta(days=45)).isoformat(timespec="seconds")
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE memory_facts SET updated_at=? WHERE user_id='u1'", (old,))
    assert store.recall("u1") == []
    # 过期之后新值可以写入，不算冲突。
    written, conflicts = store.remember("u1", [(ENV_OS, "Linux")])
    assert written == [ENV_OS]
    assert conflicts == []


def test_forget_removes_one_or_all(store):
    store.remember("u1", [(ENV_OS, "Windows"), (ENV_VERSION, "Python 3.12")])
    assert store.forget("u1", ENV_OS) == 1
    assert [fact.key for fact in store.recall("u1")] == [ENV_VERSION]
    assert store.forget("u1") == 1
    assert store.recall("u1") == []


def test_render_block_includes_conflict_lines(store):
    store.remember("u1", [(ENV_OS, "Windows")])
    _, conflicts = store.remember("u1", [(ENV_OS, "Linux")])
    block = render_block(store.recall("u1"), conflicts)
    assert "- env.os: Windows" in block
    assert "先向用户确认" in block
    assert "记录为 Windows，本次说的是 Linux" in block


def test_render_block_is_empty_without_facts():
    assert render_block([], []) == ""
