"""飞书文档读取层：解析单个 wiki/docx 链接，读取元信息和纯文本。

复用飞书入口的凭据、TLS 1.2、代理和超时配置，直接调用只读云文档 API。
输出供后续 RAG 使用，本层不调用模型、不遍历知识库，也不修改飞书内容。
"""

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from run_feishu import FeishuHTTPTransport, FeishuSettings


class FeishuDocumentError(RuntimeError):
    """读取失败时保留阶段和飞书错误码，便于区分权限、链接及网络问题。"""


@dataclass(frozen=True)
class FeishuDocument:
    """一次读取的正文与来源；不包含应用凭据或 access token。"""

    document_id: str
    title: str
    source_url: str
    content: str
    revision_id: int | None


def parse_document_url(url: str) -> tuple[str, str, str]:
    """识别页面链接，去掉分享参数；wiki 节点 token 与底层文档 ID 分开处理。"""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    parts = parsed.path.strip("/").split("/")
    if (
        parsed.scheme != "https"
        or not (host == "feishu.cn" or host.endswith(".feishu.cn"))
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or len(parts) != 2
        or parts[0] not in {"wiki", "docx"}
        or not re.fullmatch(r"[A-Za-z0-9]+", parts[1])
    ):
        raise ValueError("请提供 https://组织.feishu.cn/wiki/页面编号 或 /docx/文档编号")
    return parts[0], parts[1], urlunsplit(("https", host, "/" + "/".join(parts), "", ""))


class FeishuDocumentReader:
    """使用应用身份读取已授权页面；API 请求始终发往飞书官方 API 域名。"""

    def __init__(self, settings: FeishuSettings) -> None:
        self.settings = settings
        self.http = FeishuHTTPTransport(settings)

    def _request(self, stage: str, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """统一检查 HTTP 与业务错误，不把凭据或整份响应写进错误日志。"""
        try:
            response = self.http.request(
                method, "https://open.feishu.cn/open-apis" + path, **kwargs
            )
        except self.http.requests.RequestException as exc:
            raise FeishuDocumentError(
                f"{stage}网络失败（{type(exc).__name__}），请检查网络/TLS/代理"
            ) from None
        try:
            payload = response.json()
        except ValueError:
            raise FeishuDocumentError(
                f"{stage}返回非 JSON 响应（HTTP {response.status_code}）"
            ) from None
        if not isinstance(payload, dict):
            raise FeishuDocumentError(f"{stage}返回了无法识别的响应结构")
        if response.status_code != 200 or payload.get("code") != 0:
            # 错误码和 log ID 足以关联飞书后台日志；不打印可能包含敏感信息的响应正文。
            raise FeishuDocumentError(
                f"{stage}失败：HTTP {response.status_code}，code={payload.get('code')}，"
                f"log_id={response.headers.get('X-Tt-Logid', '无')}。"
                "请检查应用 API 权限已生效，以及目标页面的“添加文档应用”可阅读授权。"
            )
        if payload.get("data") is not None and not isinstance(payload["data"], dict):
            raise FeishuDocumentError(f"{stage}返回了无法识别的 data 结构")
        return payload

    def read(self, url: str) -> FeishuDocument:
        """解析节点后读取 docx 元信息与纯文本；不支持的页面类型明确报错。"""
        kind, token, source_url = parse_document_url(url)
        auth = self._request(
            "应用认证",
            "POST",
            "/auth/v3/tenant_access_token/internal",
            json={
                "app_id": self.settings.FEISHU_APP_ID,
                "app_secret": self.settings.FEISHU_APP_SECRET.get_secret_value(),
            },
        )
        access_token = auth.get("tenant_access_token")
        if not isinstance(access_token, str) or not access_token:
            raise FeishuDocumentError("应用认证响应缺少 tenant_access_token")
        headers = {"Authorization": f"Bearer {access_token}"}
        if kind == "wiki":
            payload = self._request(
                "解析知识库节点",
                "GET",
                "/wiki/v2/spaces/get_node",
                headers=headers,
                params={"token": token, "obj_type": "wiki"},
            )
            node = (payload.get("data") or {}).get("node") or {}
            if not isinstance(node, dict):
                raise FeishuDocumentError("飞书返回的知识库节点结构不完整")
            if node.get("obj_type") != "docx":
                raise FeishuDocumentError(
                    f"当前仅读取新版 docx 文档，该节点类型为 {node.get('obj_type')}"
                )
            token = node.get("obj_token", "")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9]+", token):
            raise FeishuDocumentError("飞书返回的底层文档 ID 无效")
        metadata = self._request(
            "读取文档信息", "GET", f"/docx/v1/documents/{token}", headers=headers
        )
        document = (metadata.get("data") or {}).get("document") or {}
        if not isinstance(document, dict):
            raise FeishuDocumentError("飞书返回的文档信息结构不完整")
        raw = self._request(
            "读取文档正文", "GET", f"/docx/v1/documents/{token}/raw_content", headers=headers
        )
        content = (raw.get("data") or {}).get("content")
        title = document.get("title")
        if not isinstance(content, str) or not isinstance(title, str) or not title:
            raise FeishuDocumentError("飞书返回的文档标题或正文结构不完整")
        return FeishuDocument(token, title, source_url, content, document.get("revision_id"))
