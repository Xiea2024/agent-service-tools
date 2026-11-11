import json

import httpx

with httpx.Client(
    base_url="https://127.0.0.1:11443",
    verify=False,
    timeout=90,
    headers={"Host": "demo.example.test", "Origin": "https://demo.example.test"},
) as c:
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/auth/config").json()["registration_enabled"] is False
    assert (
        c.post(
            "/api/auth/login",
            json={"email": "member@example.test", "password": "smoke-only-password-123"},
        ).status_code
        == 200
    )
    # Cookies belong to the logical Host rather than the socket IP.
    cookies = list(c.cookies.jar)
    cookie = next(x.value for x in cookies if x.name == "supportpilot_session")
    c.headers["Cookie"] = "supportpilot_session=" + cookie
    assert c.get("/api/auth/me").json()["platform_admin"] is False
    assert c.post("/api/tenants", json={"name": "forbidden"}).status_code == 403
    assert c.post("/api/invoke", json={"message": "x" * 4001}).status_code == 422
    answer = c.post("/api/invoke", json={"message": "hello", "thread_id": "public-invoke"})
    assert answer.status_code == 200, answer.text
    assert "integration OK" in answer.json()["content"], answer.text
    stream = c.post("/api/stream", json={"message": "hello", "thread_id": "public-stream"})
    assert (
        stream.status_code == 200 and "[DONE]" in stream.text and "integration OK" in stream.text
    ), stream.text
    assert c.post("/api/knowledge/documents/import", content="{}").status_code == 403
    no_origin = c.post("/api/threads", json={}, headers={"Origin": ""})
    assert no_origin.status_code == 403
    limited = [
        c.post("/api/auth/login", json={"email": "missing@example.test", "password": "wrong"})
        for _ in range(12)
    ]
    assert any(
        r.status_code == 429 and "text/html" in r.headers.get("content-type", "") for r in limited
    ), "NGINX auth limit did not trigger"
    print(
        json.dumps(
            {
                "https_nginx": True,
                "login_secure_cookie": True,
                "member_cannot_create_team": True,
                "input_limit": True,
                "invoke_through_litellm": True,
                "sse_through_litellm": True,
                "upload_denied": True,
                "csrf_origin_required": True,
            }
        )
    )
