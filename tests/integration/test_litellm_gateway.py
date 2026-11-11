"""Real LiteLLM + PostgreSQL + Redis + synthetic upstream, never paid models."""

import os
import time

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("GATEWAY_TEST_URL"), reason="gateway test stack required"
)


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=os.environ["GATEWAY_TEST_URL"], timeout=30) as client:
        for _ in range(90):
            try:
                if client.get("/health/liveliness").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2)
        else:
            pytest.fail("LiteLLM did not become ready")
        yield client


def key(client, **limits):
    result = client.post(
        "/key/generate",
        headers={"Authorization": "Bearer sk-ci-only-master-key"},
        json={
            "models": ["demo-chat", "demo-embedding"],
            "max_budget": 10,
            "budget_duration": "24h",
            **limits,
        },
    )
    assert result.status_code == 200, result.text
    return {"Authorization": "Bearer " + result.json()["key"]}


def test_chat_stream_embedding_tool_call_and_denied_models(client):
    headers = key(client)
    body = {"model": "demo-chat", "messages": [{"role": "user", "content": "test"}]}
    response = client.post("/v1/chat/completions", headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert "integration OK" in response.json()["choices"][0]["message"]["content"]
    response = client.post("/v1/chat/completions", headers=headers, json=body | {"stream": True})
    assert (
        response.status_code == 200
        and "[DONE]" in response.text
        and "integration OK" in response.text
    )
    response = client.post(
        "/v1/embeddings", headers=headers, json={"model": "demo-embedding", "input": "test"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["embedding"] == [1.0, 0.0, 0.0]
    response = client.post(
        "/v1/chat/completions",
        headers=headers,
        json=body
        | {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                        },
                    },
                }
            ],
            "tool_choice": "required",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "lookup"
    assert client.post(
        "/v1/chat/completions", headers=headers, json=body | {"model": "expensive-forbidden"}
    ).status_code in {400, 401, 403}
    assert client.post("/key/generate", headers=headers, json={}).status_code in {400, 401, 403}


def test_budget_denies_and_missing_key_denies(client):
    body = {"model": "demo-chat", "messages": [{"role": "user", "content": "test"}]}
    assert client.post("/v1/chat/completions", json=body).status_code == 401
    assert client.post(
        "/v1/chat/completions", headers=key(client, max_budget=0), json=body
    ).status_code in {400, 401, 402, 422, 429}
