"""飞书接入层的离线测试：验证消息过滤、快速回执和发送失败隔离。

使用合成事件、模拟 HTTP 后端与 Mock 发送函数，不读取真实凭据或调用 LLM。
这些测试不代表飞书应用权限、发布状态或真实网络已验收。
"""

import json
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import NAMESPACE_URL, uuid5

import pytest
from pydantic import ValidationError

import run_feishu
from run_feishu import (
    AGENT_ERROR_TEXT,
    REPLY_TEXT,
    FeishuHandler,
    FeishuMessage,
    FeishuSettings,
    configure_feishu_network,
    create_agent_reply,
)


def message_event(**changes):
    """生成飞书接收消息 v2.0 的最小单聊文本事件。"""
    message = {
        "message_id": "om_test",
        "chat_id": "oc_test",
        "chat_type": "p2p",
        "message_type": "text",
        "content": '{"text":"你好"}',
    }
    message.update(changes)
    return {"event": {"sender": {"sender_type": "user"}, "message": message}}


@pytest.mark.parametrize(
    "data",
    [
        {},
        message_event(chat_type="group"),
        message_event(message_type="image"),
        message_event(content="not json"),
        message_event(content="[]"),
        message_event(content='{"text":" "}'),
        message_event(content='{"text":123}'),
        message_event(message_id=""),
        message_event(message_id=123),
        message_event(chat_id=""),
        {"event": {"sender": {"sender_type": "app"}}},
    ],
)
def test_unsupported_events_do_not_send(data):
    sender = Mock()
    handler = FeishuHandler(sender)
    handler.on_event(data)
    assert handler._queue.empty()
    sender.assert_not_called()


def test_event_returns_before_network_reply_finishes():
    entered, release, returned = Event(), Event(), Event()

    def slow_send(message_id, text):
        assert message_id == "om_test"
        assert text == REPLY_TEXT
        entered.set()
        release.wait(timeout=2)

    handler = FeishuHandler(slow_send)
    handler.start()

    def receive():
        handler.on_event(message_event())
        returned.set()

    callback = Thread(target=receive, daemon=True)
    try:
        callback.start()
        assert entered.wait(timeout=2)
        assert returned.wait(timeout=0.5)
        assert not release.is_set()
    finally:
        release.set()
        callback.join(timeout=2)
        handler.stop()


def test_send_failure_is_logged_and_next_message_is_processed(caplog):
    finished = Event()
    calls = []

    def send(message_id, text):
        calls.append(message_id)
        if len(calls) == 1:
            raise RuntimeError("synthetic failure")
        finished.set()

    handler = FeishuHandler(send)
    handler.start()
    try:
        handler.on_event(message_event())
        handler.on_event(message_event(message_id="om_next"))
        assert finished.wait(timeout=2)
        assert calls == ["om_test", "om_next"]
        assert "飞书回复失败" in caplog.text
    finally:
        handler.stop()


def test_full_queue_reports_failure_instead_of_silently_dropping():
    handler = FeishuHandler(Mock())
    for number in range(32):
        handler.on_event(message_event(message_id=f"om_{number}"))
    with pytest.raises(RuntimeError, match="队列已满"):
        handler.on_event(message_event(message_id="om_overflow"))
    # 入队失败没有占用去重记录；腾出空间后，同一个事件可以再次投递。
    handler._queue.get_nowait()
    handler._queue.task_done()
    handler.on_event(message_event(message_id="om_overflow"))
    assert "om_overflow" in handler._seen


def test_agent_bridge_passes_text_auth_timeout_and_stable_sessions(monkeypatch):
    """从事件回调走到真实 AgentClient，用模拟 HTTP 后端验证两轮上下文路由。"""
    import httpx

    settings = FeishuSettings(
        _env_file=None,
        FEISHU_APP_ID="cli_test",
        FEISHU_APP_SECRET="fake",
        FEISHU_AGENT_URL="http://127.0.0.1:8080/",
        FEISHU_AGENT_TIMEOUT=72,
        AUTH_SECRET="fake-backend-secret",
    )
    requests, histories, replies = [], {}, []
    finished = Event()

    def post(url, **kwargs):
        requests.append((url, kwargs))
        body = kwargs["json"]
        history = histories.setdefault(body["thread_id"], [])
        history.append(body["message"])
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"type": "ai", "content": " / ".join(history)},
        )

    def send(message_id, text):
        replies.append((message_id, text))
        if len(replies) == 3:
            finished.set()

    monkeypatch.setattr(httpx, "post", post)
    reply = create_agent_reply(settings)
    handler = FeishuHandler(send, reply)
    handler.start()
    try:
        handler.on_event(message_event(content='{"text":"订单接口返回500"}'))
        handler.on_event(
            message_event(message_id="om_second", content='{"text":"升级到v1.2后出现"}')
        )
        handler.on_event(message_event())  # 重投首条消息，不应重复调用模型。
        handler.on_event(message_event(message_id="om_other", chat_id="oc_other"))
        assert finished.wait(timeout=3)
        assert replies == [
            ("om_test", "订单接口返回500"),
            ("om_second", "订单接口返回500 / 升级到v1.2后出现"),
            ("om_other", "你好"),
        ]
    finally:
        handler.stop()

    assert len(requests) == 3
    first, second, other = [options["json"] for _, options in requests]
    assert first["thread_id"] == second["thread_id"] != other["thread_id"]
    assert first["user_id"] == second["user_id"] != other["user_id"]
    # 原业务会话仍在数据库中；新的 Agent 命名空间避免误用旧上下文。
    old_thread = str(uuid5(NAMESPACE_URL, json.dumps(["feishu", "cli_test", "oc_test"])))
    assert first["thread_id"] != old_thread
    for url, options in requests:
        assert url == "http://127.0.0.1:8080/dev-issue-agent/invoke"
        assert options["timeout"] == 72
        assert options["headers"] == {"Authorization": "Bearer fake-backend-secret"}
    # 重新构建客户端仍映射回同一会话，应用 ID 改变则使用另一会话。
    create_agent_reply(settings)(FeishuMessage("om_restart", "oc_test", "继续"))
    assert requests[-1][1]["json"]["thread_id"] == first["thread_id"]
    create_agent_reply(settings.model_copy(update={"FEISHU_APP_ID": "cli_other"}))(
        FeishuMessage("om_app", "oc_test", "你好")
    )
    assert requests[-1][1]["json"]["thread_id"] != first["thread_id"]


def test_duplicate_events_are_skipped_while_agent_is_running_and_after_reply():
    """模型执行中的重投也必须去重，回调始终快速返回。"""
    entered, release, finished = Event(), Event(), Event()
    calls, replies = [], []

    def generate(message):
        calls.append(message.message_id)
        if message.message_id == "om_test":
            entered.set()
            release.wait(timeout=3)
        return message.text

    def send(message_id, text):
        replies.append(message_id)
        if message_id == "om_next":
            finished.set()

    handler = FeishuHandler(send, generate)
    handler.start()
    try:
        handler.on_event(message_event())
        assert entered.wait(timeout=2)
        handler.on_event(message_event())
        assert calls == ["om_test"]
        assert handler._queue.empty()
        release.set()
        handler._queue.join()
        handler.on_event(message_event())
        handler.on_event(message_event(message_id="om_next"))
        assert finished.wait(timeout=2)
        assert calls == replies == ["om_test", "om_next"]
    finally:
        release.set()
        handler.stop()


@pytest.mark.parametrize("failure", ["timeout", "http_error", "empty", "wrong_type", "malformed"])
def test_agent_failure_sends_hint_without_retry_and_worker_recovers(monkeypatch, caplog, failure):
    import httpx

    settings = FeishuSettings(_env_file=None, FEISHU_APP_ID="cli_test", FEISHU_APP_SECRET="fake")
    calls, replies = [], []
    finished = Event()

    def post(url, **kwargs):
        calls.append(kwargs["json"])
        request = httpx.Request("POST", url)
        if len(calls) == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("synthetic timeout", request=request)
            if failure == "http_error":
                return httpx.Response(503, request=request)
            body = {
                "empty": {"type": "ai", "content": " "},
                "wrong_type": {"type": "tool", "content": "tool result"},
                "malformed": {},
            }[failure]
            return httpx.Response(200, request=request, json=body)
        return httpx.Response(200, request=request, json={"type": "ai", "content": "已恢复"})

    def send(message_id, text):
        replies.append((message_id, text))
        if message_id == "om_next":
            finished.set()

    monkeypatch.setattr(httpx, "post", post)
    handler = FeishuHandler(send, create_agent_reply(settings))
    handler.start()
    try:
        handler.on_event(message_event())
        handler.on_event(message_event(message_id="om_next"))
        assert finished.wait(timeout=2)
        assert replies == [("om_test", AGENT_ERROR_TEXT), ("om_next", "已恢复")]
        assert len(calls) == 2  # 失败不自动重试，避免后端重复写入同一条用户消息。
        assert "Agent 调用失败" in caplog.text
    finally:
        handler.stop()


def test_credentials_are_required_and_secret_is_masked(monkeypatch):
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    with pytest.raises(ValidationError):
        FeishuSettings(_env_file=None)
    settings = FeishuSettings(
        _env_file=None, FEISHU_APP_ID="cli_test", FEISHU_APP_SECRET="test-secret-value"
    )
    assert "test-secret-value" not in repr(settings)


def test_bypass_proxy_is_loaded_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("FEISHU_BYPASS_PROXY", raising=False)
    config = tmp_path / ".env"
    config.write_text(
        "FEISHU_APP_ID=cli_test\nFEISHU_APP_SECRET=fake\nFEISHU_BYPASS_PROXY=true\n",
        encoding="utf-8",
    )
    assert FeishuSettings(_env_file=config).FEISHU_BYPASS_PROXY is True


@pytest.mark.parametrize("enabled", [False, True])
def test_proxy_bypass_only_affects_feishu_and_preserves_existing_rules(monkeypatch, enabled):
    import os

    from requests.utils import get_environ_proxies

    # Windows 的真实环境变量名不区分大小写；用独立映射同时覆盖两种写法。
    monkeypatch.setattr(
        os,
        "environ",
        {
            "HTTPS_PROXY": "http://proxy.invalid:8080",
            "NO_PROXY": "localhost,127.0.0.1",
            "no_proxy": "internal.invalid",
        },
    )
    settings = FeishuSettings(
        _env_file=None,
        FEISHU_APP_ID="cli_test",
        FEISHU_APP_SECRET="fake",
        FEISHU_BYPASS_PROXY=enabled,
    )
    configure_feishu_network(settings)
    configure_feishu_network(settings)
    proxies = get_environ_proxies("https://open.feishu.cn/open-apis/auth/v3/")
    if enabled:
        assert proxies == {}
        assert os.environ["NO_PROXY"] == os.environ["no_proxy"]
        assert os.environ["NO_PROXY"].split(",") == [
            "internal.invalid",
            "localhost",
            "127.0.0.1",
            "open.feishu.cn",
        ]
    else:
        assert proxies["https"] == "http://proxy.invalid:8080"
        assert os.environ["NO_PROXY"] == "localhost,127.0.0.1"
        assert os.environ["no_proxy"] == "internal.invalid"
    assert get_environ_proxies("https://other.invalid")["https"] == "http://proxy.invalid:8080"
    assert os.environ["HTTPS_PROXY"] == "http://proxy.invalid:8080"


@pytest.mark.parametrize("exit_reason", [None, KeyboardInterrupt, RuntimeError])
def test_main_applies_proxy_bypass_and_drains_tasks_on_exit(monkeypatch, exit_reason):
    import asyncio
    import os

    from requests.utils import get_environ_proxies

    settings = FeishuSettings(
        _env_file=None,
        FEISHU_APP_ID="cli_test",
        FEISHU_APP_SECRET="fake",
        FEISHU_BYPASS_PROXY=True,
    )
    monkeypatch.setattr(run_feishu, "FeishuSettings", lambda: settings)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("NO_PROXY", "localhost")
    monkeypatch.delenv("no_proxy", raising=False)
    state = {}

    async def cache_cron():
        try:
            await asyncio.sleep(3600)
        finally:
            state["cleaned"] = True

    def run_sdk(settings):
        # 验证真正的入口在 SDK 导入/建连前应用配置，而非只单测配置函数。
        assert get_environ_proxies("https://open.feishu.cn/callback/ws/endpoint") == {}
        assert os.environ["HTTPS_PROXY"] == "http://proxy.invalid:8080"
        loop = asyncio.get_event_loop()
        state["loop"] = loop
        state["task"] = loop.create_task(cache_cron())
        loop.run_until_complete(asyncio.sleep(0))
        if exit_reason:
            raise exit_reason("synthetic exit")

    monkeypatch.setattr(run_feishu, "_run_feishu", run_sdk)
    if exit_reason is RuntimeError:
        with pytest.raises(RuntimeError, match="synthetic exit"):
            run_feishu.main()
    else:
        run_feishu.main()

    assert state["cleaned"] is True
    assert state["task"].cancelled()
    assert state["loop"].is_closed()


def test_main_closes_loop_when_sdk_is_missing(monkeypatch):
    import asyncio

    settings = FeishuSettings(_env_file=None, FEISHU_APP_ID="cli_test", FEISHU_APP_SECRET="fake")
    monkeypatch.setattr(run_feishu, "FeishuSettings", lambda: settings)
    loops = []

    def missing_sdk(name):
        loops.append(asyncio.get_event_loop())
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(run_feishu, "import_module", missing_sdk)
    with pytest.raises(SystemExit, match="uv sync --frozen --group feishu"):
        run_feishu.main()
    assert len(loops) == 1
    assert loops[0].is_closed()


@pytest.mark.parametrize("bypass", [False, True])
@pytest.mark.parametrize("tls12", [False, True])
def test_feishu_http_uses_configured_proxy_tls_and_bounded_timeout(monkeypatch, bypass, tls12):
    import ssl

    import requests

    settings = FeishuSettings(
        _env_file=None,
        FEISHU_APP_ID="cli_test",
        FEISHU_APP_SECRET="fake",
        FEISHU_BYPASS_PROXY=bypass,
        FEISHU_FORCE_TLS12=tls12,
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    for name in ("NO_PROXY", "no_proxy", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        monkeypatch.delenv(name, raising=False)
    calls = []

    def send(adapter, request, **kwargs):
        calls.append((adapter, request, kwargs))
        response = requests.Response()
        response.status_code = 200
        response._content = b"{}"
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    transport = run_feishu.FeishuHTTPTransport(settings)
    endpoint = "https://open.feishu.cn/callback/ws/endpoint"
    transport.post(endpoint, json={"AppID": "cli_test", "AppSecret": "fake"})
    adapter, request, options = calls[0]
    assert options["timeout"] == (5, 15)
    assert options["verify"] is True
    if bypass:
        assert options["proxies"] == {}
    else:
        assert options["proxies"]["https"] == "http://proxy.invalid:8080"
    if tls12:
        context = adapter.poolmanager.connection_pool_kw["ssl_context"]
        assert context.minimum_version == ssl.TLSVersion.TLSv1_2
        assert context.maximum_version == ssl.TLSVersion.TLSv1_2
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname
        _, pool_options = adapter.build_connection_pool_key_attributes(request, True)
        assert pool_options["ssl_context"] is context
    else:
        assert "ssl_context" not in adapter.poolmanager.connection_pool_kw

    transport.request("POST", "https://open.feishu.cn/open-apis/im/v1/messages", timeout=9)
    assert calls[-1][2]["timeout"] == 9

    # 其他域名继续走原生 Requests，不继承飞书的直连或 TLS 限制。
    transport.request("GET", "https://other.invalid")
    assert calls[-1][2]["proxies"]["https"] == "http://proxy.invalid:8080"
    assert "ssl_context" not in calls[-1][0].poolmanager.connection_pool_kw


def test_direct_feishu_http_preserves_custom_ca_bundle(monkeypatch):
    import requests

    settings = FeishuSettings(
        _env_file=None,
        FEISHU_APP_ID="cli_test",
        FEISHU_APP_SECRET="fake",
        FEISHU_BYPASS_PROXY=True,
    )
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "custom-ca.pem")
    options = []

    def send(adapter, request, **kwargs):
        options.append(kwargs)
        response = requests.Response()
        response.status_code = 200
        response._content = b"{}"
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    run_feishu.FeishuHTTPTransport(settings).post("https://open.feishu.cn/callback/ws/endpoint")
    assert options[0]["verify"] == "custom-ca.pem"


@pytest.mark.parametrize("tls12", [False, True])
def test_sdk_network_applies_to_bootstrap_api_and_websocket_and_restores(monkeypatch, tls12):
    import ssl

    import requests

    settings = FeishuSettings(
        _env_file=None,
        FEISHU_APP_ID="cli_test",
        FEISHU_APP_SECRET="fake",
        FEISHU_FORCE_TLS12=tls12,
    )

    def original_options(headers=None):
        return {"proxy": None}

    ws = SimpleNamespace(requests=requests, _ws_connect_kwargs=original_options)
    http = SimpleNamespace(requests=requests)
    modules = {
        "requests": requests,
        "lark_oapi.ws.client": ws,
        "lark_oapi.core.http.transport": http,
    }
    monkeypatch.setattr(run_feishu, "import_module", modules.__getitem__)
    with pytest.raises(RuntimeError, match="synthetic exit"):
        with run_feishu.feishu_sdk_network(settings):
            assert ws.requests is http.requests
            assert isinstance(ws.requests, run_feishu.FeishuHTTPTransport)
            # 替换 SDK 模块引用不会更改全局 Requests 的函数。
            assert requests.request is not ws.requests.request
            options = ws._ws_connect_kwargs()
            assert options["proxy"] is None
            if tls12:
                assert options["ssl"].maximum_version == ssl.TLSVersion.TLSv1_2
                assert options["ssl"].verify_mode == ssl.CERT_REQUIRED
                assert options["ssl"].check_hostname
            else:
                assert "ssl" not in options
            raise RuntimeError("synthetic exit")
    assert ws.requests is http.requests is requests
    assert ws._ws_connect_kwargs is original_options
