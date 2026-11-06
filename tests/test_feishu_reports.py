import asyncio
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from feishu_reports import FeishuReportPublisher, ReportPublishError, report_blocks


def transport(failure=None):
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        data = {}
        if url.endswith("/tenant_access_token/internal"):
            payload = {"code": 0, "tenant_access_token": "fake-token"}
        else:
            if url.endswith("/docx/v1/documents"):
                data = {"document": {"document_id": "doc123"}}
            elif url.endswith("/children"):
                data = {"children": [{} for _ in kwargs["json"]["children"]]}
            elif url.endswith("/members") and method == "GET":
                data = {"items": [{"member_type": "openid", "member_id": "user", "perm": "view"}]}
                if failure == "missing_reader":
                    data = {"items": []}
            elif (
                url.endswith("/members")
                and method == "POST"
                and failure in {"grant_response", "wrong_reader", "wrong_permission"}
            ):
                data = {
                    "member": {
                        "member_type": "openid",
                        "member_id": "other-user" if failure == "wrong_reader" else "user",
                        "perm": "none" if failure == "wrong_permission" else "view",
                    }
                }
            elif url.endswith("/batch_query"):
                data = {
                    "metas": [
                        {"doc_token": "doc123", "url": "https://tenant.feishu.cn/docx/doc123"}
                    ]
                }
            payload = {"code": 0, "data": data}
        if failure == "permission" and url.endswith("/members") and method == "POST":
            payload = {"code": 99991672, "msg": "private raw response"}
        if failure == "network":
            raise requests.ConnectionError("private request details")
        return SimpleNamespace(status_code=200, json=lambda: payload)

    return SimpleNamespace(request=Mock(side_effect=request), requests=requests), calls


def test_report_blocks_preserve_text_fences_and_bound_content():
    blocks = report_blocks(
        "# 汇总\n\n## 回答\n\n````text\n# 答案中的标题\n```python\nprint('hi')\n```\n````\n"
    )
    assert blocks[0]["block_type"] == 3 and blocks[1]["block_type"] == 4
    assert blocks[2]["text"]["elements"][0]["text_run"]["content"].startswith("# 答案中的标题")
    chunks = report_blocks("中" * 2500)
    assert [len(b["text"]["elements"][0]["text_run"]["content"]) for b in chunks] == [
        1000,
        1000,
        500,
    ]


def test_publish_writes_then_grants_and_verifies_real_link(tmp_path):
    report = tmp_path / "report.md"
    report.write_text("# 汇总\n\n本地报告正文", encoding="utf-8")
    http, calls = transport()
    created = Mock()
    publisher = FeishuReportPublisher(http, "app", "secret", "folder123")
    url = publisher.publish(report, "测试报告", "user", created)
    assert url == "https://tenant.feishu.cn/docx/doc123"
    created.assert_called_once_with("doc123")
    assert calls[1][2]["json"]["folder_token"] == "folder123"
    grant = next(c for c in calls if c[1].endswith("/members") and c[0] == "POST")
    assert grant[2]["json"] == {
        "member_type": "openid",
        "member_id": "user",
        "perm": "view",
        "type": "user",
    }
    assert calls[-1][1].endswith("/metas/batch_query")
    assert len(calls) == 6


@pytest.mark.parametrize("failure", ["permission", "missing_reader"])
def test_unconfirmed_reader_is_publication_failure_and_keeps_local_report(tmp_path, failure):
    report = tmp_path / "report.md"
    report.write_text("# 已完成的报告", encoding="utf-8")
    http, calls = transport(failure)
    created = Mock()
    with pytest.raises(ReportPublishError) as exc:
        FeishuReportPublisher(http, "app", "secret").publish(report, "报告", "user", created)
    assert "private raw response" not in str(exc.value)
    assert report.read_text("utf-8") == "# 已完成的报告"
    created.assert_called_once_with("doc123")
    assert not any(c[1].endswith("/batch_query") for c in calls)


def test_complete_grant_response_needs_no_list_permission(tmp_path):
    report = tmp_path / "report.md"
    report.write_text("# 报告", encoding="utf-8")
    http, calls = transport("grant_response")
    url = FeishuReportPublisher(http, "app", "secret").publish(report, "报告", "user", Mock())
    assert url.endswith("/docx/doc123")
    assert len(calls) == 5
    assert not any(method == "GET" and path.endswith("/members") for method, path, _ in calls)


@pytest.mark.parametrize("failure", ["wrong_reader", "wrong_permission"])
def test_authoritative_grant_response_must_match_reader_and_permission(failure):
    http, calls = transport(failure)
    with pytest.raises(ReportPublishError, match="不符合预期"):
        FeishuReportPublisher(http, "app", "secret").confirm_reader("doc123", "user")
    assert not any(path.endswith("/batch_query") for _, path, _ in calls)


def test_confirm_existing_report_never_creates_document_or_writes_content():
    http, calls = transport("grant_response")
    url = FeishuReportPublisher(http, "app", "secret").confirm_reader("doc123", "user")
    assert url.endswith("/docx/doc123")
    assert len(calls) == 3
    assert all(not path.endswith(("/children", "/docx/v1/documents")) for _, path, _ in calls)


def test_network_failure_does_not_leak_request_details(tmp_path):
    report = tmp_path / "report.md"
    report.write_text("报告", encoding="utf-8")
    http, _ = transport("network")
    with pytest.raises(ReportPublishError, match="网络失败") as exc:
        FeishuReportPublisher(http, "app", "secret").publish(report, "报告", "user", Mock())
    assert "secret" not in str(exc.value) and "private" not in str(exc.value)


def test_publish_batches_blocks_and_respects_shutdown(tmp_path):
    report = tmp_path / "report.md"
    report.write_text("\n\n".join(f"# 标题 {i}" for i in range(51)), encoding="utf-8")
    http, calls = transport()
    publisher = FeishuReportPublisher(http, "app", "secret")
    publisher.publish(report, "报告", "user", Mock())
    sizes = [len(c[2]["json"]["children"]) for c in calls if c[1].endswith("/children")]
    assert sizes == [50, 1]
    stop = Event()
    stop.set()
    before = len(calls)
    with pytest.raises(asyncio.CancelledError):
        publisher.publish(report, "报告", "user", Mock(), stop)
    assert len(calls) == before


def test_api_scope_hint_does_not_expose_full_error_payload():
    http = SimpleNamespace(
        requests=requests,
        request=Mock(
            return_value=SimpleNamespace(
                status_code=400,
                json=lambda: {
                    "code": 99991672,
                    "msg": "private raw response: [docx:document:create, docx:document]",
                },
            )
        ),
    )
    with pytest.raises(ReportPublishError) as exc:
        FeishuReportPublisher(http, "app", "secret")._request(
            "创建报告", "POST", "/docx/v1/documents"
        )
    assert "docx:document:create" in str(exc.value)
    assert "private raw response" not in str(exc.value)
