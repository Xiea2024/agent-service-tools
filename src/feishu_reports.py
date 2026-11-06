"""Publish local reports as native text blocks and grant the initiator read access."""

import asyncio
import re
from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import Any
from urllib.parse import urlsplit


class ReportPublishError(RuntimeError):
    pass


def report_blocks(markdown: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    buffer: list[str] = []
    fence = ""

    def text(content: str) -> None:
        for offset in range(0, len(content), 1000):
            blocks.append(
                {
                    "block_type": 2,
                    "text": {
                        "elements": [{"text_run": {"content": content[offset : offset + 1000]}}]
                    },
                }
            )

    def flush() -> None:
        if buffer:
            text("\n".join(buffer))
            buffer.clear()

    for line in markdown.splitlines():
        marker = re.match(r"^(`{3,}|~{3,})", line)
        if marker and (not fence or marker[1] == fence):
            flush()
            fence = "" if fence else marker[1]
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", line) if not fence else None
        if heading:
            flush()
            level = len(heading[1])
            blocks.append(
                {
                    "block_type": level + 2,
                    f"heading{level}": {"elements": [{"text_run": {"content": heading[2][:1000]}}]},
                }
            )
        elif not line and not fence:
            flush()
        else:
            buffer.append(line)
    flush()
    if not blocks:
        raise ValueError("报告正文为空")
    return blocks


class FeishuReportPublisher:
    def __init__(self, http: Any, app_id: str, app_secret: str, folder_token: str = ""):
        self.http, self.app_id, self.app_secret = http, app_id, app_secret
        self.folder_token = folder_token

    def _request(self, stage: str, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self.http.request(
                method, "https://open.feishu.cn/open-apis" + path, **kwargs
            )
        except self.http.requests.RequestException as exc:
            raise ReportPublishError(f"{stage}网络失败：{type(exc).__name__}") from None
        try:
            payload = response.json()
        except ValueError:
            raise ReportPublishError(f"{stage}返回非 JSON，HTTP {response.status_code}") from None
        if not isinstance(payload, dict) or response.status_code != 200 or payload.get("code") != 0:
            code = payload.get("code") if isinstance(payload, dict) else "unknown"
            scopes = []
            if isinstance(payload, dict):
                scopes = sorted(
                    set(
                        re.findall(
                            r"\b(?:docx|drive|docs):[a-z0-9_:]+\b", str(payload.get("msg", ""))
                        )
                    )
                )
            scope_hint = f"；接口要求权限：{', '.join(scopes)}" if scopes else ""
            raise ReportPublishError(
                f"{stage}失败：HTTP {response.status_code}，code={code}{scope_hint}；请核对该接口的应用权限和目标文件授权"
            )
        return payload

    def _authenticate(self, stop: Event) -> dict[str, str]:
        if stop.is_set():
            raise asyncio.CancelledError
        auth = self._request(
            "应用认证",
            "POST",
            "/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        token = auth.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise ReportPublishError("应用认证响应缺少访问凭证")
        return {"Authorization": f"Bearer {token}"}

    def publish(
        self,
        report_path: Path,
        title: str,
        owner_open_id: str,
        on_created: Callable[[str], None],
        stop: Event | None = None,
    ) -> str:
        blocks = report_blocks(report_path.read_text(encoding="utf-8"))
        if not owner_open_id.strip():
            raise ValueError("缺少报告阅读人")
        stop = stop or Event()

        def request(stage: str, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            if stop.is_set():
                raise asyncio.CancelledError
            return self._request(stage, method, path, **kwargs)

        headers = self._authenticate(stop)
        body = {"title": title}
        if self.folder_token:
            body["folder_token"] = self.folder_token
        created = request("创建报告", "POST", "/docx/v1/documents", headers=headers, json=body)
        document_id = (created.get("data") or {}).get("document", {}).get("document_id")
        if not isinstance(document_id, str) or not re.fullmatch(r"[A-Za-z0-9]+", document_id):
            raise ReportPublishError("创建报告响应缺少合法 document_id")
        on_created(document_id)
        for offset in range(0, len(blocks), 50):
            if offset and stop.wait(0.4):
                raise asyncio.CancelledError
            result = request(
                "写入报告",
                "POST",
                f"/docx/v1/documents/{document_id}/blocks/{document_id}/children",
                headers=headers,
                json={"children": blocks[offset : offset + 50], "index": -1},
            )
            children = (result.get("data") or {}).get("children")
            if not isinstance(children, list) or len(children) != len(blocks[offset : offset + 50]):
                raise ReportPublishError("写入报告返回的块数量不完整")
        return self._confirm_reader(document_id, owner_open_id, headers, stop)

    def confirm_reader(
        self, document_id: str, owner_open_id: str, stop: Event | None = None
    ) -> str:
        """Confirm access to an already written report without creating or appending content."""
        if not re.fullmatch(r"[A-Za-z0-9]+", document_id) or not owner_open_id.strip():
            raise ValueError("缺少合法文档编号或报告阅读人")
        stop = stop or Event()
        return self._confirm_reader(document_id, owner_open_id, self._authenticate(stop), stop)

    def _confirm_reader(
        self, document_id: str, owner_open_id: str, headers: dict[str, str], stop: Event
    ) -> str:
        def request(stage: str, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            if stop.is_set():
                raise asyncio.CancelledError
            return self._request(stage, method, path, **kwargs)

        permission_path = f"/drive/v1/permissions/{document_id}/members"
        granted = request(
            "授予发起人阅读权限",
            "POST",
            permission_path,
            headers=headers,
            params={"type": "docx", "need_notification": "false"},
            json={
                "member_type": "openid",
                "member_id": owner_open_id,
                "perm": "view",
                "type": "user",
            },
        )
        member = (granted.get("data") or {}).get("member")

        def matches(item: Any) -> bool:
            return (
                isinstance(item, dict)
                and item.get("member_type") == "openid"
                and item.get("member_id") == owner_open_id
                and item.get("perm") in ("view", "edit", "full_access")
            )

        complete = isinstance(member, dict) and all(
            isinstance(member.get(key), str) for key in ("member_type", "member_id", "perm")
        )
        if complete:
            if not matches(member):
                raise ReportPublishError("授权响应中的阅读人与权限不符合预期")
        else:
            members = request(
                "核对阅读权限", "GET", permission_path, headers=headers, params={"type": "docx"}
            )
            items = (members.get("data") or {}).get("items") or []
            if not any(matches(item) for item in items):
                raise ReportPublishError("未能确认发起人的直接阅读权限")
        metadata = request(
            "读取报告链接",
            "POST",
            "/drive/v1/metas/batch_query",
            headers=headers,
            json={
                "request_docs": [{"doc_token": document_id, "doc_type": "docx"}],
                "with_url": True,
            },
        )
        metas = (metadata.get("data") or {}).get("metas") or []
        url = next(
            (
                item.get("url")
                for item in metas
                if isinstance(item, dict) and item.get("doc_token") == document_id
            ),
            None,
        )
        if not isinstance(url, str):
            raise ReportPublishError("无法获取报告访问链接")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not (parsed.hostname or "").endswith(".feishu.cn")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.path != f"/docx/{document_id}"
        ):
            raise ReportPublishError("报告访问链接格式不合法")
        return url
