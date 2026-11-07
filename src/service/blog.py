"""SQLite-backed personal blog. Mounted behind the service's existing bearer auth."""

import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

router = APIRouter(prefix="/blog", tags=["blog"])


def database_path() -> Path:
    return Path(os.environ.get("BLOG_DB_PATH", Path(__file__).resolve().parents[2] / ".local/blog.sqlite3"))


def connect() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS blog_posts (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        content TEXT NOT NULL,
        published INTEGER NOT NULL DEFAULT 0 CHECK (published IN (0, 1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        published_at TEXT
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS blog_posts_recent ON blog_posts(published, published_at DESC)")
    db.commit()
    return db


def initialize_blog() -> None:
    with closing(connect()):
        pass


class PostInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=500)
    content: str = Field(min_length=1, max_length=200_000)
    published: bool = False


@router.get("/posts")
def list_posts(limit: int = Query(100, ge=1, le=100), drafts: bool = False):
    with closing(connect()) as db:
        rows = db.execute(
            "SELECT id, title, summary, published, created_at, updated_at, published_at FROM blog_posts "
            + ("" if drafts else "WHERE published = 1 ")
            + "ORDER BY COALESCE(published_at, created_at) DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return {"posts": [dict(row) for row in rows]}


@router.get("/posts/{post_id}")
def get_post(post_id: str):
    with closing(connect()) as db:
        row = db.execute("SELECT * FROM blog_posts WHERE id = ?", (post_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "文章不存在")
        return dict(row)


def save_post(post: PostInput, post_id: str | None = None):
    title, content = post.title.strip(), post.content.strip()
    if not title or not content:
        raise HTTPException(422, "标题和正文不能为空")
    now = datetime.now(timezone.utc).isoformat()
    with closing(connect()) as db, db:
        previous = None
        if post_id:
            previous = db.execute("SELECT * FROM blog_posts WHERE id = ?", (post_id,)).fetchone()
            if previous is None:
                raise HTTPException(404, "文章不存在")
        post_id = post_id or str(uuid4())
        published_at = (previous["published_at"] if previous else None) or (now if post.published else None)
        db.execute("""INSERT INTO blog_posts
            (id, title, summary, content, published, created_at, updated_at, published_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET title=excluded.title, summary=excluded.summary,
                content=excluded.content, published=excluded.published,
                updated_at=excluded.updated_at, published_at=excluded.published_at""",
            (post_id, title, post.summary.strip(), content, post.published,
             previous["created_at"] if previous else now, now, published_at))
    return get_post(post_id)


@router.post("/posts", status_code=201)
def create_post(post: PostInput):
    return save_post(post)


@router.put("/posts/{post_id}")
def update_post(post_id: str, post: PostInput):
    return save_post(post, post_id)


@router.delete("/posts/{post_id}")
def delete_post(post_id: str):
    with closing(connect()) as db, db:
        if not db.execute("DELETE FROM blog_posts WHERE id = ?", (post_id,)).rowcount:
            raise HTTPException(404, "文章不存在")
    return {"deleted": True}
