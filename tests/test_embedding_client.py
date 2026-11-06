"""硅基流动 Embedding 的离线测试：验证独立配置、原文请求和失败反馈。

HTTP 使用 MockTransport，CLI 使用合成向量；不读取真实密钥、不调用外部 API。
Mock 向量仅用于协议验证，不代表 BGE-M3 的真实维度或检索效果。
"""

import json
import runpy
import ssl
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from openai import APIConnectionError, APITimeoutError, AuthenticationError, RateLimitError
from pydantic import ValidationError

import embedding_client
from embedding_client import EmbeddingSettings, create_embedding_http_client, create_embeddings


@pytest.mark.parametrize("enabled", [False, True])
def test_tls_compatibility_keeps_certificate_verification(enabled, monkeypatch):
    """协议兼容只能限制 TLS 版本，不能关闭证书或主机名校验。"""
    settings = EmbeddingSettings(
        _env_file=None, EMBEDDING_API_KEY="fake-only", EMBEDDING_FORCE_TLS12=enabled
    )
    factory = Mock()
    monkeypatch.setattr(embedding_client, "DefaultHttpxClient", factory)
    create_embedding_http_client(settings)
    context = factory.call_args.kwargs["verify"]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    if enabled:
        assert context.minimum_version == context.maximum_version == ssl.TLSVersion.TLSv1_2
    else:
        assert context.maximum_version == ssl.TLSVersion.MAXIMUM_SUPPORTED


def test_embeddings_reuse_tls_factory_only_when_requested(monkeypatch):
    """普通调用保持原 SDK 行为；启用兼容后，入库与查询采用相同传输配置。"""
    settings = EmbeddingSettings(
        _env_file=None, EMBEDDING_API_KEY="fake-only", EMBEDDING_FORCE_TLS12=True
    )
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
        factory = Mock(return_value=client)
        monkeypatch.setattr(embedding_client, "create_embedding_http_client", factory)
        embeddings = create_embeddings(settings)
        factory.assert_called_once_with(settings)
        assert embeddings.http_client is client


def test_settings_read_dotenv_and_mask_key(tmp_path, monkeypatch):
    for key in EmbeddingSettings.model_fields:
        monkeypatch.delenv(key, raising=False)
    config = tmp_path / ".env"
    config.write_text("EMBEDDING_API_KEY=fake-embedding-secret\n", encoding="utf-8")
    settings = EmbeddingSettings(_env_file=config)
    assert settings.EMBEDDING_MODEL == "BAAI/bge-m3"
    assert str(settings.EMBEDDING_BASE_URL) == "https://api.siliconflow.cn/v1"
    assert "fake-embedding-secret" not in repr(settings)
    with pytest.raises(ValidationError):
        EmbeddingSettings(_env_file=None)


def test_documents_and_query_use_same_provider_and_send_raw_text():
    settings = EmbeddingSettings(
        _env_file=None,
        EMBEDDING_API_KEY="fake-embedding-secret",
        EMBEDDING_TIMEOUT=12,
        EMBEDDING_MAX_RETRIES=0,
    )
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert str(request.url) == "https://api.siliconflow.cn/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer fake-embedding-secret"
        assert request.extensions["timeout"]["read"] == 12
        assert body["model"] == "BAAI/bge-m3"
        assert body["encoding_format"] == "float"
        assert "dimensions" not in body
        assert all(isinstance(text, str) for text in body["input"])
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "BAAI/bge-m3",
                "data": [
                    {"object": "embedding", "index": index, "embedding": [0.1, 0.2, 0.3]}
                    for index in range(len(body["input"]))
                ],
                "usage": {"prompt_tokens": 5, "total_tokens": 5},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        embeddings = create_embeddings(settings, http_client=client)
        assert embeddings.embed_documents(["订单接口返回500", "启动时缺少依赖"]) == [
            [0.1, 0.2, 0.3],
            [0.1, 0.2, 0.3],
        ]
        assert embeddings.embed_query("接口为什么报错？") == [0.1, 0.2, 0.3]
    assert calls[0]["input"] == ["订单接口返回500", "启动时缺少依赖"]
    assert calls[1]["input"] == ["接口为什么报错？"]


def test_authentication_failure_is_not_retried():
    settings = EmbeddingSettings(_env_file=None, EMBEDDING_API_KEY="fake-embedding-secret")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, json={"error": {"message": "invalid key"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AuthenticationError):
            create_embeddings(settings, http_client=client).embed_query("测试")
    assert len(calls) == 1


def run_cli(monkeypatch, *, vector=None, error=None):
    """仅替换向量 API 调用，让 CLI 的格式与错误处理走真实代码。"""
    settings = EmbeddingSettings(_env_file=None, EMBEDDING_API_KEY="fake-embedding-secret")
    model = SimpleNamespace(embed_query=Mock(return_value=vector, side_effect=error))
    monkeypatch.setattr(embedding_client, "EmbeddingSettings", lambda: settings)
    monkeypatch.setattr(embedding_client, "create_embeddings", lambda settings: model)
    monkeypatch.setattr("sys.argv", ["check_embedding.py"])
    script = Path(__file__).resolve().parents[1] / "scripts" / "check_embedding.py"
    runpy.run_path(str(script), run_name="__main__")
    return model


def test_cli_reports_actual_dimension_and_hides_key(monkeypatch, capsys):
    model = run_cli(monkeypatch, vector=[0.1, 0.2, 0.3])
    output = capsys.readouterr().out
    assert "实际向量维度：3" in output
    assert "Embedding 连通验证成功" in output
    assert "fake-embedding-secret" not in output
    model.embed_query.assert_called_once()


@pytest.mark.parametrize("vector", [[], [float("nan")], [float("inf")], [True], ["bad"]])
def test_cli_rejects_unusable_vectors(monkeypatch, capsys, vector):
    with pytest.raises(SystemExit) as error:
        run_cli(monkeypatch, vector=vector)
    assert error.value.code == 1
    assert "有效的有限数值向量" in capsys.readouterr().err


@pytest.mark.parametrize("failure", ["timeout", "connection", "auth", "rate_limit", "malformed"])
def test_cli_reports_failures_without_response_body_or_key(monkeypatch, capsys, failure):
    request = httpx.Request("POST", "https://api.siliconflow.cn/v1/embeddings")
    if failure == "timeout":
        exception = APITimeoutError(request=request)
    elif failure == "connection":
        exception = APIConnectionError(message="fake-embedding-secret", request=request)
    elif failure in {"auth", "rate_limit"}:
        status = 401 if failure == "auth" else 429
        error_type = AuthenticationError if failure == "auth" else RateLimitError
        exception = error_type(
            "fake-embedding-secret", response=httpx.Response(status, request=request), body={}
        )
    else:
        exception = IndexError("fake-embedding-secret")
    with pytest.raises(SystemExit) as error:
        run_cli(monkeypatch, error=exception)
    assert error.value.code == 1
    output = capsys.readouterr()
    assert "fake-embedding-secret" not in output.err
    assert "验证成功" not in output.out
