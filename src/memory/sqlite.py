"""本地状态存储：使用 LangGraph AsyncSqliteSaver 将 checkpoint 保存到 SQLite。

Store 使用 InMemoryStore，属于内存实现，服务重启后不会保留其中的数据。
checkpoint 和 Store 是两类存储；它们都不同于 RAG 使用的 Chroma 文档向量索引。
"""

from contextlib import AbstractAsyncContextManager, asynccontextmanager

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.memory import InMemoryStore

from core.settings import settings


def get_sqlite_saver() -> AbstractAsyncContextManager[AsyncSqliteSaver]:
    """Initialize and return a SQLite saver instance."""
    return AsyncSqliteSaver.from_conn_string(settings.SQLITE_DB_PATH)


class AsyncInMemoryStore:
    """Wrapper for InMemoryStore that provides an async context manager interface."""

    def __init__(self):
        self.store = InMemoryStore()

    async def __aenter__(self):
        return self.store

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # No cleanup needed for InMemoryStore
        pass

    async def setup(self):
        # No-op method for compatibility with PostgresStore
        pass


@asynccontextmanager
async def get_sqlite_store():
    """Initialize and return a store instance for long-term memory.

    Note: SQLite-specific store isn't available in LangGraph,
    so we use InMemoryStore wrapped in an async context manager for compatibility.
    """
    store_manager = AsyncInMemoryStore()
    yield await store_manager.__aenter__()
