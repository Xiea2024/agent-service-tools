"""Exercise the blog router independently of agent/model startup."""
import importlib.util
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

spec = importlib.util.spec_from_file_location("blog_under_test", Path(__file__).parents[2] / "src/service/blog.py")
blog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(blog)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOG_DB_PATH", str(tmp_path / "blog.sqlite3"))
    app = FastAPI()
    app.include_router(blog.router)
    with TestClient(app) as client:
        yield client


def test_draft_publish_edit_unpublish_and_delete(client):
    assert client.get("/blog/posts").json() == {"posts": []}
    payload = {"title": "第一篇", "content": "# 你好\n\n正文", "summary": "摘要"}
    response = client.post("/blog/posts", json=payload)
    assert response.status_code == 201
    post = response.json()
    assert post["published_at"] is None
    assert client.get("/blog/posts").json()["posts"] == []
    assert len(client.get("/blog/posts?drafts=true").json()["posts"]) == 1
    url = f"/blog/posts/{post['id']}"
    published = client.put(url, json={**payload, "published": True}).json()
    assert published["published_at"]
    assert client.get("/blog/posts?limit=5").json()["posts"][0]["id"] == post["id"]
    updated = client.put(url, json={**payload, "title": "更新标题", "published": True}).json()
    assert updated["published_at"] == published["published_at"]
    assert updated["created_at"] == post["created_at"]
    assert client.get(url).json()["content"] == payload["content"]
    assert client.put(url, json=payload).status_code == 200
    assert not client.get("/blog/posts").json()["posts"]
    assert client.delete(url).status_code == 200
    assert client.get(url).status_code == 404
    assert client.put(url, json=payload).status_code == 404


def test_recent_posts_order_limit_and_validation(client):
    for number in range(6):
        client.post("/blog/posts", json={"title": f"文章{number}", "content": "正文", "published": True})
    recent = client.get("/blog/posts?limit=5").json()["posts"]
    assert len(recent) == 5
    assert recent[0]["title"] == "文章5"
    assert client.post("/blog/posts", json={"title": "  ", "content": "正文"}).status_code == 422
    assert client.post("/blog/posts", json={"title": "标题", "content": ""}).status_code == 422
    assert client.get("/blog/posts?limit=0").status_code == 422
    assert client.delete("/blog/posts/missing").status_code == 404


def test_sqlite_persists_between_connections(client):
    post = client.post("/blog/posts", json={"title": "保存", "content": "持久化", "published": True}).json()
    blog.initialize_blog()
    with blog.closing(blog.connect()) as db:
        assert db.execute("SELECT content FROM blog_posts WHERE id = ?", (post["id"],)).fetchone()[0] == "持久化"
