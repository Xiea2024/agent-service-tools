"""阶段记录接口：返回低基数记录，不返回问题、回答或工具正文。"""

import httpx
import pytest

from core import settings
from observability import stages
from service import app


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_SECRET", None)
    monkeypatch.setattr(settings, "LANGFUSE_TRACING", False)
    stages.clear_stages()
    yield
    stages.clear_stages()


async def get(path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.asyncio
async def test_stages_endpoint_returns_records_for_a_run():
    with stages.stage(stages.RETRIEVE, config={"run_id": "run-api"}, variant="v1") as observed:
        observed["hits"] = 1
        observed["top_score"] = 0.81
    response = await get("/runs/run-api/stages")
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == "run-api"
    assert [record["stage"] for record in body["stages"]] == ["retrieve"]
    assert body["stages"][0]["decisions"] == {"hits": 1, "top_score": 0.81}
    # 接口只暴露阶段元数据，不能把问题或回答正文带出去。
    assert "query" not in body["stages"][0]["decisions"]


@pytest.mark.asyncio
async def test_unknown_run_returns_empty_list_not_404():
    response = await get("/runs/missing-run/stages")
    assert response.status_code == 200
    assert response.json() == {"run_id": "missing-run", "stages": []}


@pytest.mark.asyncio
async def test_overlong_run_id_is_rejected():
    response = await get(f"/runs/{'a' * 65}/stages")
    assert response.status_code == 422
