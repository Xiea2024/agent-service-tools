"""单篇飞书文档读取的离线测试：验证链接解析、节点映射、权限错误和本地导出。

全部使用合成文档和模拟 HTTP 响应，不访问飞书或读取真实凭据。
真实接口读取结果另行记录，Mock 测试不代表应用已经拥有任何资源权限。
"""

import json
import runpy
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

import feishu_documents
import run_feishu
from feishu_documents import (
    FeishuDocument,
    FeishuDocumentError,
    FeishuDocumentReader,
    parse_document_url,
)
from run_feishu import FeishuSettings

WIKI_URL = "https://demo.feishu.cn/wiki/Wiki123"
DOCX_URL = "https://demo.feishu.cn/docx/Doc123"


def response(payload, status=200):
    """构造最小响应，令测试与真实网络完全隔离。"""
    return SimpleNamespace(
        status_code=status, headers={"X-Tt-Logid": "log_test"}, json=Mock(return_value=payload)
    )


@pytest.fixture
def reader(monkeypatch):
    settings = FeishuSettings(
        _env_file=None, FEISHU_APP_ID="cli_test", FEISHU_APP_SECRET="fake-secret"
    )
    replies = [
        response({"code": 0, "tenant_access_token": "fake-access-token"}),
        response({"code": 0, "data": {"node": {"obj_type": "docx", "obj_token": "Doc123"}}}),
        response({"code": 0, "data": {"document": {"title": "演示资料", "revision_id": 3}}}),
        response({"code": 0, "data": {"content": "演示说明\nDEV-ISSUE-READ-TEST-001"}}),
    ]
    http = SimpleNamespace(request=Mock(side_effect=replies), requests=requests)
    monkeypatch.setattr(feishu_documents, "FeishuHTTPTransport", lambda settings: http)
    return FeishuDocumentReader(settings), http, replies


@pytest.mark.parametrize("url", [WIKI_URL, WIKI_URL + "?from=share#heading", WIKI_URL + "/"])
def test_wiki_url_is_normalized(url):
    assert parse_document_url(url) == ("wiki", "Wiki123", WIKI_URL)


@pytest.mark.parametrize(
    "url",
    [
        "http://demo.feishu.cn/wiki/Wiki123",
        "https://demo.feishu.cn.evil.invalid/wiki/Wiki123",
        "https://evil.invalid/wiki/Wiki123",
        "https://user:password@demo.feishu.cn/wiki/Wiki123",
        "https://demo.feishu.cn:1234/wiki/Wiki123",
        "https://demo.feishu.cn/wiki/settings/123",
        "https://demo.feishu.cn/wiki/",
        "https://demo.feishu.cn/sheets/Sheet123",
        "https://demo.feishu.cn/docx/Doc%2F123",
    ],
)
def test_invalid_links_fail_before_requesting_credentials(reader, url):
    instance, http, _ = reader
    with pytest.raises(ValueError):
        instance.read(url)
    http.request.assert_not_called()


def test_wiki_resolves_document_id_and_preserves_content_and_source(reader):
    instance, http, _ = reader
    document = instance.read(WIKI_URL + "?from=share")
    assert document == FeishuDocument(
        "Doc123", "演示资料", WIKI_URL, "演示说明\nDEV-ISSUE-READ-TEST-001", 3
    )
    calls = http.request.call_args_list
    assert len(calls) == 4
    assert calls[1].kwargs["params"] == {"token": "Wiki123", "obj_type": "wiki"}
    assert calls[2].args[1].endswith("/documents/Doc123")
    assert calls[3].args[1].endswith("/documents/Doc123/raw_content")
    assert all(call.args[1].startswith("https://open.feishu.cn/open-apis/") for call in calls)
    assert all(
        call.kwargs["headers"] == {"Authorization": "Bearer fake-access-token"}
        for call in calls[1:]
    )


def test_direct_docx_skips_wiki_resolution(reader):
    instance, http, replies = reader
    http.request.side_effect = [replies[0], *replies[2:]]
    assert instance.read(DOCX_URL).document_id == "Doc123"
    assert http.request.call_count == 3


@pytest.mark.parametrize("stage", range(4))
def test_permission_errors_report_stage_code_and_log_id_without_secrets(reader, stage):
    instance, http, replies = reader
    replies[stage] = response(
        {"code": 131006, "msg": "private fake-secret fake-access-token"}, status=403
    )
    with pytest.raises(FeishuDocumentError) as error:
        instance.read(WIKI_URL)
    text = str(error.value)
    assert ["应用认证", "解析知识库节点", "读取文档信息", "读取文档正文"][stage] in text
    assert "131006" in text and "log_test" in text
    assert "fake-secret" not in text and "fake-access-token" not in text
    assert http.request.call_count == stage + 1


def test_unsupported_wiki_type_is_not_treated_as_docx(reader):
    instance, http, replies = reader
    replies[1].json.return_value["data"]["node"]["obj_type"] = "sheet"
    with pytest.raises(FeishuDocumentError, match="sheet"):
        instance.read(WIKI_URL)
    assert http.request.call_count == 2


@pytest.mark.parametrize(
    "failure", ["network", "non_json", "missing_token", "bad_data", "bad_node"]
)
def test_unusable_responses_do_not_look_like_success(reader, failure):
    instance, http, replies = reader
    if failure == "network":
        http.request.side_effect = requests.Timeout("fake-secret")
    elif failure == "non_json":
        replies[0].json.side_effect = ValueError("fake-secret")
    elif failure == "missing_token":
        replies[0].json.return_value = {"code": 0}
    elif failure == "bad_data":
        replies[1].json.return_value = {"code": 0, "data": ["bad"]}
    else:
        replies[1].json.return_value["data"]["node"] = "bad"
    with pytest.raises(FeishuDocumentError) as error:
        instance.read(WIKI_URL)
    assert "fake-secret" not in str(error.value)


@pytest.mark.parametrize("content", [None, {"bad": "content"}])
def test_missing_or_invalid_content_is_reported(reader, content):
    instance, _, replies = reader
    replies[3].json.return_value["data"]["content"] = content
    with pytest.raises(FeishuDocumentError, match="正文结构不完整"):
        instance.read(WIKI_URL)


def test_cli_saves_complete_utf8_json_without_printing_body(monkeypatch, tmp_path, capsys):
    settings = FeishuSettings(
        _env_file=None, FEISHU_APP_ID="cli_test", FEISHU_APP_SECRET="fake-secret"
    )
    document = FeishuDocument("Doc123", "演示资料", WIKI_URL, "仅保存在文件中的正文", 3)
    monkeypatch.setattr(run_feishu, "FeishuSettings", lambda: settings)
    monkeypatch.setattr(FeishuDocumentReader, "read", Mock(return_value=document))
    output = tmp_path / "nested" / "document.json"
    monkeypatch.setattr(
        "sys.argv",
        ["read_feishu_document.py", WIKI_URL, "--preview-chars", "0", "--output", str(output)],
    )
    script = Path(__file__).resolve().parents[1] / "scripts" / "read_feishu_document.py"
    runpy.run_path(str(script), run_name="__main__")
    assert json.loads(output.read_text(encoding="utf-8")) == asdict(document)
    printed = capsys.readouterr().out
    assert "演示资料" in printed
    assert document.content not in printed
    assert "fake-secret" not in printed
