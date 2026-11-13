"""长期记忆：只保存用户明确陈述过的少量结构化事实。

设计上刻意不做“向量记忆”：

- **只收三类白名单字段**：``env.os``、``env.version``、``issue.component``。
  键是固定的，值是短字符串，因此可以精确召回、可以失效、可以审计。
- **只写用户真的说过的事实**。抽取用正则而不是模型：没匹配到就不记，
  绝不根据上下文推测用户用的是 Windows 还是 Linux。
- **冲突不覆盖**。同一条事实出现不同值时保留旧值，并把冲突交给本轮回答去澄清；
  静默覆盖会让用户以为系统记住了新的环境。
- **过期不召回**。超过 TTL 的记录既不注入提示词，也会在下次写入时清掉。

存储是本地 SQLite 单文件，使用参数化查询与主键去重；多租户模式下不启用，
避免和 PostgreSQL 业务库出现两套不一致的事实来源。
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

TTL_DAYS = 30
MAX_VALUE_CHARS = 64
MAX_FACTS_IN_PROMPT = 5
MAX_EXTRACTED = 4

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / ".local/agent-memory.sqlite3"

# 白名单键：新增字段必须同时改抽取、渲染和测试，避免记忆悄悄膨胀。
ENV_OS = "env.os"
ENV_VERSION = "env.version"
ISSUE_COMPONENT = "issue.component"
KEYS: tuple[str, ...] = (ENV_OS, ENV_VERSION, ISSUE_COMPONENT)

_OS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bwindows(?:\s*(?:10|11|server\s*\d+))?\b"), "Windows"),
    (re.compile(r"(?i)\bmac\s*os(?:\s*x)?\b|\bmacos\b"), "macOS"),
    (re.compile(r"(?i)\bubuntu\b"), "Ubuntu"),
    (re.compile(r"(?i)\bdebian\b"), "Debian"),
    (re.compile(r"(?i)\bcentos\b"), "CentOS"),
    (re.compile(r"(?i)\balpine\b"), "Alpine"),
    (re.compile(r"(?i)\blinux\b"), "Linux"),
    (re.compile(r"麒麟"), "麒麟"),
    (re.compile(r"(?i)\buos\b|统信"), "统信 UOS"),
)
_VERSION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bpython\s*(\d+\.\d+(?:\.\d+)?)"), "Python {}"),
    (re.compile(r"(?i)\bnode(?:\.js)?\s*v?(\d+(?:\.\d+)?)"), "Node {}"),
    (re.compile(r"(?i)\bjdk\s*v?(\d+(?:\.\d+)?)"), "JDK {}"),
    (re.compile(r"(?i)\bjava\s*(\d+(?:\.\d+)?)"), "Java {}"),
)
_COMPONENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[A-Za-z0-9_\-./{}]*)"),
    re.compile(r"接口\s*(?:路径)?\s*[:：]?\s*(/[A-Za-z0-9_\-./{}]+)"),
)


@dataclass(frozen=True, slots=True)
class Fact:
    key: str
    value: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Conflict:
    key: str
    remembered: str
    stated: str


def _clean(value: str) -> str:
    """去掉尾随标点与空白，并限制长度；超长说明不是一条事实，直接丢弃。"""
    text = " ".join(value.split()).rstrip(".,;，。；、)）]】")
    return text[:MAX_VALUE_CHARS]


def extract_facts(text: str) -> list[tuple[str, str]]:
    """从用户这句话里抽取白名单事实；没有匹配就返回空列表，不做推测。"""
    stripped = (text or "").strip()
    if not stripped:
        return []
    found: dict[str, str] = {}

    for pattern, label in _OS_PATTERNS:
        if pattern.search(stripped):
            found[ENV_OS] = label
            break
    for pattern, template in _VERSION_PATTERNS:
        match = pattern.search(stripped)
        if match:
            found[ENV_VERSION] = template.format(match.group(1))
            break
    for pattern in _COMPONENT_PATTERNS:
        match = pattern.search(stripped)
        if match:
            groups = [group for group in match.groups() if group]
            candidate = " ".join(groups) if len(groups) > 1 else groups[0]
            found[ISSUE_COMPONENT] = _clean(candidate)
            break

    return [(key, value) for key, value in found.items() if key in KEYS and value][:MAX_EXTRACTED]


def render_block(facts: list[Fact], conflicts: list[Conflict]) -> str:
    """渲染成给模型的文本块；没有内容时返回空串，不往提示词里塞占位文本。"""
    lines: list[str] = []
    if facts:
        lines.append("已知的环境事实（用户此前明确陈述过，不是你的推断）：")
        lines.extend(f"- {fact.key}: {fact.value}" for fact in facts)
    if conflicts:
        if lines:
            lines.append("")
        lines.append("以下事实与本次陈述不一致，先向用户确认，不要直接采用新值：")
        lines.extend(
            f"- {item.key}: 记录为 {item.remembered}，本次说的是 {item.stated}"
            for item in conflicts
        )
    return "\n".join(lines)


class FactStore:
    """本地 SQLite 事实表；一个用户一条事实一行，主键去重。"""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else DEFAULT_PATH

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_facts (
                user_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                source_run_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, key)
            )
            """
        )
        return connection

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    @staticmethod
    def _expired(updated_at: str) -> bool:
        try:
            saved = datetime.fromisoformat(updated_at)
        except ValueError:
            return True
        if saved.tzinfo is None:
            saved = saved.replace(tzinfo=UTC)
        return datetime.now(UTC) - saved > timedelta(days=TTL_DAYS)

    def remember(
        self, user_id: str, facts: list[tuple[str, str]], source_run_id: str | None = None
    ) -> tuple[list[str], list[Conflict]]:
        """写入新事实；返回值是 (写入的键, 冲突列表)，冲突不会被覆盖。"""
        if not user_id or not facts:
            return [], []
        written: list[str] = []
        conflicts: list[Conflict] = []
        now = self._now()
        with closing(self._connect()) as db:
            for key, value in facts:
                if key not in KEYS:
                    continue
                row = db.execute(
                    "SELECT value, updated_at FROM memory_facts WHERE user_id=? AND key=?",
                    (user_id, key),
                ).fetchone()
                if row and not self._expired(row["updated_at"]):
                    if row["value"] == value:
                        db.execute(
                            "UPDATE memory_facts SET updated_at=?, source_run_id=? "
                            "WHERE user_id=? AND key=?",
                            (now, source_run_id, user_id, key),
                        )
                        continue
                    conflicts.append(Conflict(key, row["value"], value))
                    continue
                db.execute(
                    "INSERT INTO memory_facts (user_id, key, value, source_run_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, "
                    "source_run_id=excluded.source_run_id, updated_at=excluded.updated_at",
                    (user_id, key, value, source_run_id, now, now),
                )
                written.append(key)
            db.commit()
        return written, conflicts

    def recall(self, user_id: str, limit: int = MAX_FACTS_IN_PROMPT) -> list[Fact]:
        """按最近更新排序返回未过期的事实。"""
        if not user_id:
            return []
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT key, value, updated_at FROM memory_facts "
                "WHERE user_id=? ORDER BY updated_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [
            Fact(row["key"], row["value"], row["updated_at"])
            for row in rows
            if not self._expired(row["updated_at"])
        ]

    def forget(self, user_id: str, key: str | None = None) -> int:
        """删除一个用户的全部事实或某一项；返回删除行数。"""
        with closing(self._connect()) as db:
            if key is None:
                cursor = db.execute("DELETE FROM memory_facts WHERE user_id=?", (user_id,))
            else:
                cursor = db.execute(
                    "DELETE FROM memory_facts WHERE user_id=? AND key=?", (user_id, key)
                )
            db.commit()
            return cursor.rowcount


_STORE: FactStore | None = None


def fact_store() -> FactStore:
    """进程内单例；测试用 reset_fact_store 换成临时文件。"""
    global _STORE
    if _STORE is None:
        _STORE = FactStore()
    return _STORE


def reset_fact_store(path: Path | str | None = None) -> None:
    global _STORE
    _STORE = FactStore(path) if path else None
