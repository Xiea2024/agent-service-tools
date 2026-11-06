"""飞书渠道入口：通过官方 SDK 接收单聊文本，调用后端研发问题处理 Agent 并回复。

pydantic-settings 读取根目录 .env；模型配置和历史由现有 FastAPI 服务管理。
有界队列避免阻塞事件回执；SQL 会话目录支持菜单新建、历史切换和重启恢复。
"""

import asyncio
import json
import logging
import os
import sqlite3
import ssl
from collections import OrderedDict
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from importlib import import_module
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from pydantic import AnyHttpUrl, Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from evaluation_jobs import EvaluationJobStore, EvaluationWorker, evaluate_job
from feedback import AnswerSnapshot, FeedbackStore
from feishu_cards import history_card, notice_card, session_card
from feishu_evaluation import EvaluationAction, FeishuEvaluationUI, evaluation_card
from feishu_feedback import FeedbackAction, FeishuFeedbackUI, parse_feedback_action
from feishu_progress import FeishuProgressUI
from feishu_reports import FeishuReportPublisher
from feishu_sessions import FeishuSessionStore, legacy_thread_id
from feishu_tasks import DurableTaskWorker, MessageTask, MessageTaskStore, RedisTaskStream
from observability.identifiers import task_id_for

logger = logging.getLogger(__name__)
REPLY_TEXT = "已收到你的消息，飞书连接正常。"
AGENT_ERROR_TEXT = "Agent 暂时无法完成回复，请稍后发送一条新消息重试。"
AGENT_ID = "dev-issue-agent"


def request_not_started(exc: Exception) -> str | None:
    import httpx

    seen = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
            return type(current).__name__
        if isinstance(current, httpx.HTTPStatusError) and current.response.status_code in {
            401,
            403,
            404,
            405,
            422,
        }:
            return f"HTTP {current.response.status_code}"
        current = current.__cause__ or current.__context__
    return None


class FeishuSettings(BaseSettings):
    """读取渠道和后端连接配置；LLM 凭据仍由后端管理。"""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent / ".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )
    FEISHU_APP_ID: str = Field(min_length=1)
    FEISHU_APP_SECRET: SecretStr = Field(min_length=1)
    FEISHU_BYPASS_PROXY: bool = False
    FEISHU_FORCE_TLS12: bool = True
    FEISHU_REPLY_MODE: Literal["agent", "echo"] = "agent"
    FEISHU_AGENT_URL: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8080")
    FEISHU_AGENT_TIMEOUT: float = Field(default=75, gt=0, le=180)
    FEISHU_SESSION_DB_PATH: Path = (
        Path(__file__).resolve().parent.parent / ".local" / "feishu-sessions.sqlite3"
    )
    FEISHU_FEEDBACK_DB_PATH: Path = (
        Path(__file__).resolve().parent.parent / ".local" / "feishu-feedback.sqlite3"
    )
    FEISHU_EVAL_ENABLED: bool = True
    FEISHU_EVAL_DB_PATH: Path = (
        Path(__file__).resolve().parent.parent / ".local/evaluation-jobs.sqlite3"
    )
    FEISHU_EVAL_CASES_PATH: Path = (
        Path(__file__).resolve().parent.parent / "evals/supportpilot/cases.json"
    )
    FEISHU_EVAL_OUTPUT_ROOT: Path = (
        Path(__file__).resolve().parent.parent / "evals/supportpilot/runs"
    )
    FEISHU_EVAL_TIMEOUT: float = Field(default=90, gt=0, le=180)
    FEISHU_EVAL_MODEL: str | None = None
    FEISHU_EVAL_ALLOWED_OPEN_IDS: str = ""
    FEISHU_REPORT_FOLDER_TOKEN: str = ""
    FEISHU_QUEUE_BACKEND: Literal["memory", "redis"] = "memory"
    FEISHU_REDIS_URL: SecretStr = SecretStr("redis://127.0.0.1:6379/0")
    FEISHU_REDIS_DEDUP_TTL: int = Field(default=86400, ge=60)
    FEISHU_TASK_DB_PATH: Path = (
        Path(__file__).resolve().parent.parent / ".local/feishu-tasks.sqlite3"
    )
    AUTH_SECRET: SecretStr | None = None
    METRICS_ENABLED: bool = False
    FEISHU_METRICS_HOST: str = "127.0.0.1"
    FEISHU_METRICS_PORT: int = Field(default=9101, ge=1, le=65535)


@dataclass(frozen=True)
class FeishuMessage:
    """后台处理所需的最小消息；message_id 用于回复，chat_id 用于会话映射。"""

    message_id: str
    chat_id: str
    text: str
    open_id: str = ""


def create_agent_reply(
    settings: FeishuSettings,
    sessions: FeishuSessionStore | None = None,
    feedback: FeedbackStore | None = None,
    before_invoke: Callable[[FeishuMessage, str, str], None] | None = None,
    after_invoke: Callable[[FeishuMessage, str], None] | None = None,
) -> Callable[[FeishuMessage], str | AnswerSnapshot]:
    """复用现有 AgentClient，让后端承担模型调用和 checkpoint 管理。"""
    from client import AgentClient

    # 不在初始化时请求 /info；后端故障交给工作线程反馈，飞书仍可正常接收事件。
    client = AgentClient(
        base_url=str(settings.FEISHU_AGENT_URL).rstrip("/"),
        timeout=settings.FEISHU_AGENT_TIMEOUT,
        get_info=False,
    )
    client.update_agent(AGENT_ID, verify=False)
    # pydantic-settings 不会把 .env 导出为环境变量，需显式传给现有客户端。
    client.auth_secret = settings.AUTH_SECRET.get_secret_value() if settings.AUTH_SECRET else None
    if settings.FEISHU_AGENT_URL.host in {"localhost", "127.0.0.1", "[::1]"}:
        # 本地后端无需经过系统代理，避免 Clash 配置影响 127.0.0.1 的 HTTP 请求。
        hosts = ",".join(os.getenv(key, "") for key in ("no_proxy", "NO_PROXY"))
        for key in ("no_proxy", "NO_PROXY"):
            os.environ[key] = ",".join(
                dict.fromkeys([*filter(None, hosts.split(",")), settings.FEISHU_AGENT_URL.host])
            )

    def reply(message: FeishuMessage) -> str | AnswerSnapshot:
        if feedback:
            if not message.open_id:
                raise ValueError("缺少用户标识，无法关联回答反馈")
            cached = feedback.find_answer(
                message.message_id, message.chat_id, message.open_id, message.text
            )
            if cached:
                return cached
        # 首次登记沿用原 UUID5；之后用 SQL 保存的当前线程，不覆盖用户的新选择。
        # 这里仅做单聊会话映射，不把 thread_id 当作后端的用户权限校验。
        original_thread = legacy_thread_id(settings.FEISHU_APP_ID, AGENT_ID, message.chat_id)
        thread_id = (
            sessions.current(message.chat_id, message.open_id).thread_id
            if sessions
            else original_thread
        )
        # 用户标识不随新会话变化；同一个用户可以拥有多个 thread_id。
        user_id = f"feishu:{original_thread}"
        if before_invoke:
            before_invoke(message, thread_id, user_id)
        task_id = task_id_for(json.dumps([settings.FEISHU_APP_ID, AGENT_ID]), message.message_id)
        response = client.invoke(
            message.text, thread_id=thread_id, user_id=user_id, task_id=task_id
        )
        if response.type != "ai" or not response.content.strip():
            raise ValueError("研发问题处理 Agent 未返回有效的 AI 文本回复")
        snapshot = None
        if feedback:
            snapshot = feedback.save_answer(
                source_message_id=message.message_id,
                chat_id=message.chat_id,
                open_id=message.open_id,
                user_id=user_id,
                thread_id=thread_id,
                run_id=response.run_id or "",
                question=message.text,
                answer=response.content,
            )
            logger.info(
                "回答快照已保存：snapshot_id=%s run_id=%s", snapshot.snapshot_id, snapshot.run_id
            )
        if after_invoke:
            after_invoke(message, response.run_id or "")
        if sessions:
            sessions.record_turn(message.chat_id, thread_id, message.text)
        logger.info("Agent 回复完成：chat_id=%s thread_id=%s", message.chat_id, thread_id)
        return snapshot if snapshot else response.content

    return reply


def configure_feishu_network(settings: FeishuSettings) -> None:
    """按配置仅让飞书 API 直连，保留其他域名的代理及证书校验。"""
    if not settings.FEISHU_BYPASS_PROXY:
        return
    # requests 优先读取小写 no_proxy；合并两种写法，避免覆盖已有排除规则。
    hosts = [
        host.strip()
        for key in ("no_proxy", "NO_PROXY")
        for host in os.environ.get(key, "").split(",")
        if host.strip()
    ]
    # NO_PROXY 不是浏览器代理规则，Requests 不支持 *.feishu.cn 这种通配符。
    hosts = list(dict.fromkeys([*hosts, "open.feishu.cn"]))
    for key in ("no_proxy", "NO_PROXY"):
        os.environ[key] = ",".join(hosts)
    logger.info("已为 open.feishu.cn 启用直连，保留 TLS 证书校验")


def create_feishu_ssl_context() -> ssl.SSLContext:
    """限制到已验证可用的 TLS 1.2；CA 由 Requests / WebSocket 分别加载。"""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    return context


class FeishuHTTPTransport:
    """仅为飞书请求设置直连、TLS 和超时，不修改全局 requests 模块。"""

    def __init__(self, settings: FeishuSettings) -> None:
        self.settings = settings
        self.requests = import_module("requests")

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        if urlsplit(url).hostname != "open.feishu.cn":
            return self.requests.request(method, url, **kwargs)
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = (5, 15)
        # trust_env=False 同时禁用环境证书发现，因此显式保留自定义 CA 配置。
        kwargs.setdefault(
            "verify", os.getenv("REQUESTS_CA_BUNDLE") or os.getenv("CURL_CA_BUNDLE") or True
        )
        with self.requests.Session() as session:
            if self.settings.FEISHU_BYPASS_PROXY:
                session.trust_env = False
            if self.settings.FEISHU_FORCE_TLS12:
                context = create_feishu_ssl_context()

                class TLS12Adapter(self.requests.adapters.HTTPAdapter):
                    def init_poolmanager(self, *args: Any, **pool_kwargs: Any) -> Any:
                        pool_kwargs["ssl_context"] = context
                        return super().init_poolmanager(*args, **pool_kwargs)

                    def build_connection_pool_key_attributes(
                        self, request: Any, verify: Any, cert: Any = None
                    ) -> Any:
                        host, pool_kwargs = super().build_connection_pool_key_attributes(
                            request, verify, cert
                        )
                        # 兼容会为单个请求提供默认 SSLContext 的 Requests 版本。
                        pool_kwargs["ssl_context"] = context
                        return host, pool_kwargs

                session.mount("https://open.feishu.cn/", TLS12Adapter())
            return session.request(method, url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs)


@contextmanager
def feishu_sdk_network(settings: FeishuSettings):
    """SDK 1.7.5 未开放网络适配器；仅替换两个 SDK 模块的 Requests 引用。"""
    ws: Any = import_module("lark_oapi.ws.client")
    http: Any = import_module("lark_oapi.core.http.transport")
    original_ws_requests, original_http_requests = ws.requests, http.requests
    original_ws_kwargs = ws._ws_connect_kwargs
    transport = FeishuHTTPTransport(settings)

    def ws_connect_kwargs(*args: Any, **kwargs: Any) -> dict[str, Any]:
        options = original_ws_kwargs(*args, **kwargs)
        if settings.FEISHU_FORCE_TLS12:
            context = create_feishu_ssl_context()
            context.load_default_certs()
            options["ssl"] = context
        return options

    try:
        ws.requests = http.requests = transport
        ws._ws_connect_kwargs = ws_connect_kwargs
        if settings.FEISHU_FORCE_TLS12:
            logger.info("飞书 HTTPS / WebSocket 使用 TLS 1.2 兼容模式，保留证书与主机名校验")
        yield
    finally:
        ws.requests, http.requests = original_ws_requests, original_http_requests
        ws._ws_connect_kwargs = original_ws_kwargs


@dataclass(frozen=True)
class FeishuAction:
    """菜单/卡片操作，与普通消息共用队列，保持接收顺序和会话切换顺序。"""

    event_id: str
    open_id: str
    action: str
    chat_id: str = ""
    thread_id: str = ""


class FeishuSessionUI:
    """将只包含用户 ID 的菜单事件转换为有归属验证的 SQL 操作和卡片反馈。"""

    def __init__(
        self,
        store: FeishuSessionStore,
        send_card: Callable[[str, str, dict[str, Any], str], tuple[str, str]],
        update_card: Callable[[str, dict[str, Any]], None],
    ) -> None:
        self.store, self.send_card, self.update_card = store, send_card, update_card

    def handle(self, action: FeishuAction) -> None:
        """在工作线程中操作数据库/API，不在 SDK 的 3 秒回调窗口里等待。"""
        loading_message = ""
        try:
            chat_id = self.store.chat_for_user(action.open_id)
            if action.chat_id and action.chat_id != chat_id:
                raise ValueError("请在你与机器人的原单聊中操作，转发卡片不能切换会话。")
            if not chat_id:
                # 菜单没有 chat_id。首次给点击者发一张加载卡片，用官方返回值登记单聊，
                # 随后更新同一张卡片；无需要求用户发送额外的初始化消息。
                loading_message, chat_id = self.send_card(
                    "open_id",
                    action.open_id,
                    notice_card("会话管理", "正在加载你的会话……"),
                    f"{action.event_id}:loading",
                )
            current = self.store.current(chat_id, action.open_id)
            title = "当前会话"
            if action.action in {"session_new", "session_switch"}:
                if action.action == "session_switch" and not action.thread_id:
                    raise ValueError("缺少待继续的会话编号")
                current = self.store.change(
                    chat_id,
                    action.event_id,
                    action.thread_id if action.action == "session_switch" else None,
                )
                title = "会话已就绪"
            if action.action == "session_history":
                card = history_card(self.store.recent(chat_id), current.thread_id, chat_id)
            else:
                card = session_card(current, chat_id, title)
            logger.info(
                "会话操作完成：action=%s chat_id=%s thread_id=%s",
                action.action,
                chat_id,
                current.thread_id,
            )
        except (ValueError, sqlite3.Error):
            logger.exception("会话操作失败")
            card = notice_card(
                "操作未完成", "无法完成会话操作。请从机器人单聊菜单重试，并检查后台日志。"
            )
        if loading_message:
            self.update_card(loading_message, card)
        else:
            self.send_card("open_id", action.open_id, card, action.event_id)


class FeishuHandler:
    """过滤、去重后快速入队，由单个工作线程按接收顺序调用 Agent 并回复。"""

    def __init__(
        self,
        send_reply: Callable[[str, str], None],
        generate_reply: Callable[[FeishuMessage], str | AnswerSnapshot] | None = None,
        session_ui: FeishuSessionUI | None = None,
        feedback_ui: FeishuFeedbackUI | None = None,
        evaluation_ui: FeishuEvaluationUI | None = None,
    ) -> None:
        self._send_reply = send_reply
        self._generate_reply = generate_reply or (lambda message: REPLY_TEXT)
        self._session_ui = session_ui
        self._feedback_ui = feedback_ui
        self._evaluation_ui = evaluation_ui
        self._queue: Queue[FeishuMessage | FeishuAction | FeedbackAction | EvaluationAction] = (
            Queue(maxsize=32)
        )
        # 只记住本进程最近 1024 个已入队 ID；避免事件重投重复消耗模型额度。
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seen_lock = Lock()
        self._stop = Event()
        self._worker = Thread(target=self._run, name="feishu-reply", daemon=True)
        self.tasks: DurableTaskWorker | None = None
        self.progress: FeishuProgressUI | None = None

    def enable_redis(self, settings: FeishuSettings) -> None:
        namespace = json.dumps([settings.FEISHU_APP_ID, AGENT_ID])
        store = MessageTaskStore(settings.FEISHU_TASK_DB_PATH, namespace)
        if self._feedback_ui:
            self.progress = FeishuProgressUI(store, self._feedback_ui)
        self.tasks = DurableTaskWorker(
            store,
            RedisTaskStream.connect(
                settings.FEISHU_REDIS_URL.get_secret_value(),
                namespace,
                settings.FEISHU_REDIS_DEDUP_TTL,
            ),
            self._handle_durable,
            on_saved=self.progress.schedule if self.progress else None,
        )

    def _enqueue(
        self, key: str, item: FeishuMessage | FeishuAction | FeedbackAction | EvaluationAction
    ) -> None:
        """去重与入队共用一把锁，队列满时不登记，保留事件重投机会。"""
        if self.tasks:
            self.tasks.enqueue(key, type(item).__name__, asdict(item))
            return
        with self._seen_lock:
            if key in self._seen:
                return
            try:
                self._queue.put_nowait(item)
            except Full as exc:
                raise RuntimeError("飞书回复队列已满，请稍后重试") from exc
            self._seen[key] = None
            if len(self._seen) > 1024:
                self._seen.popitem(last=False)

    def on_menu_event(self, data: dict[str, Any]) -> None:
        """三个后台 event_key 固定对应程序操作，不经过 LLM 猜测。"""
        event, header = data.get("event") or {}, data.get("header") or {}
        open_id = ((event.get("operator") or {}).get("operator_id") or {}).get("open_id")
        action, event_id = event.get("event_key"), header.get("event_id")
        if (
            isinstance(open_id, str)
            and open_id
            and isinstance(action, str)
            and action
            and isinstance(event_id, str)
            and event_id
        ):
            if self._evaluation_ui and action in {"eval_start", "eval_status"}:
                self._enqueue(f"action:{event_id}", EvaluationAction(event_id, open_id, action))
            elif self._session_ui and action in {
                "session_new",
                "session_history",
                "session_current",
            }:
                self._enqueue(f"action:{event_id}", FeishuAction(event_id, open_id, action))

    def on_card_action(self, data: dict[str, Any]) -> dict[str, Any]:
        """快速返回 Toast；成功只表示已入队，SQL 完成后另发真实状态卡片。"""
        event, header = data.get("event") or {}, data.get("header") or {}
        value = (event.get("action") or {}).get("value") or {}
        if not isinstance(value, dict):
            return {"toast": {"type": "error", "content": "无效的会话操作"}}
        if value.get("action") == "eval_status":
            owner = (event.get("operator") or {}).get("open_id")
            event_id, job_id = header.get("event_id"), value.get("job_id")
            if not self._evaluation_ui or any(
                not isinstance(item, str) or not item for item in (owner, event_id, job_id)
            ):
                return {"toast": {"type": "error", "content": "无法查询评测任务"}}
            try:
                assert (
                    isinstance(event_id, str) and isinstance(owner, str) and isinstance(job_id, str)
                )
                self._enqueue(
                    f"action:{event_id}", EvaluationAction(event_id, owner, "eval_status", job_id)
                )
            except RuntimeError:
                return {"toast": {"type": "error", "content": "处理队列已满，请稍后重试"}}
            return {"toast": {"type": "info", "content": "正在查询已保存的评测状态"}}
        if value.get("action") == "feedback_submit":
            if not self._feedback_ui:
                return {"toast": {"type": "error", "content": "当前未启用回答反馈"}}
            try:
                feedback_action = parse_feedback_action(data)
                self._enqueue(f"feedback:{feedback_action.event_id}", feedback_action)
            except ValueError as exc:
                return {"toast": {"type": "error", "content": str(exc)}}
            except RuntimeError:
                return {"toast": {"type": "error", "content": "处理队列已满，请稍后重试"}}
            return {"toast": {"type": "info", "content": "反馈已接收，保存后将更新卡片"}}
        open_id = (event.get("operator") or {}).get("open_id")
        chat_id = (event.get("context") or {}).get("open_chat_id")
        action, event_id = value.get("action"), header.get("event_id")
        thread_id = value.get("thread_id", "")
        valid = (
            self._session_ui
            and isinstance(open_id, str)
            and open_id
            and isinstance(chat_id, str)
            and chat_id
            and isinstance(action, str)
            and action
            and isinstance(event_id, str)
            and event_id
            and action in {"session_new", "session_history", "session_current", "session_switch"}
            and value.get("chat_id") == chat_id
            and isinstance(thread_id, str)
        )
        if not valid:
            return {"toast": {"type": "error", "content": "请从原单聊的会话卡片操作"}}
        try:
            assert isinstance(event_id, str) and isinstance(open_id, str)
            assert (
                isinstance(action, str) and isinstance(chat_id, str) and isinstance(thread_id, str)
            )
            self._enqueue(
                f"action:{event_id}", FeishuAction(event_id, open_id, action, chat_id, thread_id)
            )
        except RuntimeError:
            return {"toast": {"type": "error", "content": "处理队列已满，请稍后重试"}}
        return {"toast": {"type": "info", "content": "操作已接收，正在处理"}}

    def on_event(self, data: dict[str, Any]) -> None:
        """SDK 回调只处理用户单聊文本；群聊、机器人和其他消息类型暂时忽略。"""
        event = data.get("event") or {}
        message = event.get("message") or {}
        sender = event.get("sender") or {}
        if (
            sender.get("sender_type") != "user"
            or message.get("chat_type") != "p2p"
            or message.get("message_type") != "text"
        ):
            return
        try:
            content = json.loads(message.get("content", ""))
        except (TypeError, json.JSONDecodeError):
            logger.warning("忽略格式不正确的文本消息")
            return
        text = content.get("text") if isinstance(content, dict) else None
        message_id = message.get("message_id")
        chat_id = message.get("chat_id")
        if (
            not isinstance(text, str)
            or not text.strip()
            or not isinstance(message_id, str)
            or not message_id.strip()
            or not isinstance(chat_id, str)
            or not chat_id.strip()
        ):
            return
        open_id = (sender.get("sender_id") or {}).get("open_id", "")
        if not isinstance(open_id, str):
            return
        evaluation_command = {"/eval": "eval_start", "/eval-status": "eval_status"}.get(
            text.strip()
        )
        if self._evaluation_ui and open_id and evaluation_command:
            self._enqueue(
                message_id, EvaluationAction(f"message:{message_id}", open_id, evaluation_command)
            )
            return
        command = {
            "/new": "session_new",
            "/history": "session_history",
            "/current": "session_current",
        }.get(text.strip())
        if self._session_ui and open_id and command:
            self._enqueue(message_id, FeishuAction(f"message:{message_id}", open_id, command))
        else:
            self._enqueue(message_id, FeishuMessage(message_id, chat_id, text, open_id))

    def start(self) -> None:
        """显式启动工作线程，导入模块不会建连或发送消息。"""
        if self.tasks:
            try:
                self.tasks.start(before_start=self.progress.start if self.progress else None)
            except BaseException:
                if self.progress:
                    self.progress.stop()
                raise
        else:
            self._worker.start()

    def stop(self) -> None:
        """等待当前发送结束；内存队列不提供重启后的任务恢复。"""
        if self.tasks:
            self.tasks.stop(after_join=self.progress.stop if self.progress else None)
            return
        self._stop.set()
        # 等当前请求及回复结束后再撤销 SDK 网络适配；两条网络路径均设置了超时。
        self._worker.join()

    def _handle_action(self, item) -> bool:
        if isinstance(item, EvaluationAction):
            if self._evaluation_ui:
                self._evaluation_ui.handle(item)
            return True
        if isinstance(item, FeedbackAction):
            if self._feedback_ui:
                self._feedback_ui.handle(item)
            return True
        if isinstance(item, FeishuAction):
            if self._session_ui:
                self._session_ui.handle(item)
            return True
        return False

    def _handle_durable(self, task: MessageTask) -> None:
        if not self.tasks:
            raise RuntimeError("未配置持久任务工作器")
        store = self.tasks.store
        kinds = {
            kind.__name__: kind
            for kind in (FeishuMessage, FeishuAction, FeedbackAction, EvaluationAction)
        }
        item = kinds[task.kind](**task.payload)
        if not isinstance(item, FeishuMessage):
            ui = {
                FeishuAction: self._session_ui,
                FeedbackAction: self._feedback_ui,
                EvaluationAction: self._evaluation_ui,
            }[type(item)]
            if ui is None:
                raise RuntimeError("任务对应功能未启用，请核对启动配置")
            self._handle_action(item)
            return
        snapshot = (
            self._feedback_ui.store.find_answer(
                item.message_id, item.chat_id, item.open_id, item.text
            )
            if self._feedback_ui
            else None
        )
        if snapshot and self._feedback_ui:
            store.record_agent_result(
                item.message_id, snapshot.run_id, snapshot_id=snapshot.snapshot_id
            )
            store.update(
                task.task_id,
                "ready",
                reply={"snapshot_id": snapshot.snapshot_id},
                event_type="answer_reused",
            )
            self._send_snapshot(task, snapshot)
            return
        if task.reply:
            if "snapshot_id" in task.reply:
                raise ValueError("回答快照不存在，请检查反馈数据库配置")
            self._send_reply(item.message_id, task.reply["text"])
            return
        if task.state == "invoking" or store.uncertain_chat(item.chat_id, task.task_id):
            state = "needs_review" if task.state == "invoking" else "blocked"
            error = (
                "原调用结果不明，未自动重试"
                if state == "needs_review"
                else "前序任务待确认，本条未调用 Agent"
            )
            store.update(task.task_id, state, error=error)
            if self.progress:
                self.progress.wake.set()
            else:
                self._send_reply(
                    item.message_id,
                    "上次处理的执行结果待确认，本条未重新调用模型，请联系维护者核对任务记录。",
                )
            return
        store.update(task.task_id, "invoking")
        if self.progress:
            self.progress.wake.set()
        try:
            reply = self._generate_reply(item)
        except Exception as exc:
            recovered = (
                self._feedback_ui.store.find_answer(
                    item.message_id, item.chat_id, item.open_id, item.text
                )
                if self._feedback_ui
                else None
            )
            if recovered and self._feedback_ui:
                store.record_agent_result(
                    item.message_id, recovered.run_id, snapshot_id=recovered.snapshot_id
                )
                store.update(
                    task.task_id,
                    "ready",
                    reply={"snapshot_id": recovered.snapshot_id},
                    event_type="answer_reused",
                )
                self._send_snapshot(task, recovered)
                return
            not_started = request_not_started(exc)
            if not_started:
                store.update(
                    task.task_id,
                    "failed",
                    error=f"请求未发送或被拒绝：{not_started}；检查后端连接和认证",
                )
            else:
                # A read timeout cannot prove that the backend stopped writing to its checkpoint.
                store.update(
                    task.task_id,
                    "needs_review",
                    error=f"Agent 调用异常（{type(exc).__name__}），结果待确认；未自动重试",
                )
            logger.exception(
                "Agent 调用失败：task_id=%s request_not_started=%s", task.task_id, bool(not_started)
            )
            if self.progress:
                self.progress.wake.set()
            else:
                text = (
                    "暂时无法调用问答服务，本条未执行。请稍后重新提问，或联系维护者。"
                    if not_started
                    else "Agent 调用结果暂无法确认，未自动重试；请联系维护者核对任务记录。"
                )
                self._send_reply(item.message_id, text)
            return
        saved_reply = (
            {"snapshot_id": reply.snapshot_id}
            if isinstance(reply, AnswerSnapshot)
            else {"text": reply}
        )
        store.update(task.task_id, "ready", reply=saved_reply)
        if isinstance(reply, AnswerSnapshot):
            if not self._feedback_ui:
                raise ValueError("回答快照缺少卡片发送配置")
            self._send_snapshot(task, reply)
        else:
            self._send_reply(item.message_id, reply)

    def _send_snapshot(self, task: MessageTask, snapshot: AnswerSnapshot):
        if self.progress:
            self.progress.answer(task.task_id, snapshot)
        elif self._feedback_ui:
            self._feedback_ui.send_answer(snapshot)
        else:
            raise ValueError("回答快照缺少卡片发送配置")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                message = self._queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                if not isinstance(message, FeishuMessage):
                    self._handle_action(message)
                    continue
                try:
                    reply = self._generate_reply(message)
                except Exception:
                    # 超时后后端可能仍在运行，不自动重试模型请求，防止重复写入历史。
                    logger.exception("Agent 调用失败，请检查后端服务、模型配置和请求超时")
                    reply = AGENT_ERROR_TEXT
                if isinstance(reply, AnswerSnapshot):
                    if not self._feedback_ui:
                        raise ValueError("回答快照缺少卡片发送配置")
                    self._feedback_ui.send_answer(reply)
                else:
                    self._send_reply(message.message_id, reply)
                logger.info("已发送飞书回复")
            except Exception:
                # 工作线程保留错误日志并继续处理后续消息，不把失败伪装成发送成功。
                logger.exception("飞书回复失败，请根据异常区分网络连接故障与 API 错误码")
            finally:
                self._queue.task_done()


def main() -> None:
    """加载凭据和 SDK，注册接收消息事件，然后保持 WebSocket 连接。"""
    logging.basicConfig(level=logging.INFO)
    try:
        settings = FeishuSettings()
    except ValidationError as exc:
        fields = ", ".join(str(error["loc"][0]) for error in exc.errors(include_input=False))
        raise SystemExit(f"请检查根目录 .env 中的配置：{fields}") from None

    configure_feishu_network(settings)

    # SDK 导入时会保存事件循环，必须在首次导入前设置，并在退出时清理。
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        _run_feishu(settings)
    except KeyboardInterrupt:
        logger.info("正在停止飞书接入程序")
    finally:
        # loop.close() 不会取消任务；先等待缓存清理等任务完成取消，避免析构时
        # 再向已关闭的循环调度回调（Task was destroyed / Event loop is closed）。
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
        finally:
            loop.close()
            asyncio.set_event_loop(None)


def _run_feishu(settings: FeishuSettings) -> None:
    """在入口创建的事件循环中加载并运行 SDK。"""
    try:
        # 可选依赖延迟导入，普通聊天和本模块的离线测试不需要安装飞书 SDK。
        logger.info("正在加载飞书 SDK，首次加载可能较慢。")
        lark = import_module("lark_oapi")
        im = import_module("lark_oapi.api.im.v1")
    except ModuleNotFoundError as exc:
        raise SystemExit("请先安装飞书依赖：uv sync --frozen --group feishu") from exc

    # SDK 自带日志处理器，继续向根日志器传播会让每条连接日志打印两遍。
    logging.getLogger("Lark").propagate = False
    with feishu_sdk_network(settings):
        _serve_feishu(settings, lark, im)


def _serve_feishu(settings: FeishuSettings, lark: Any, im: Any) -> None:
    """构建客户端并运行；网络配置由外层上下文负责恢复。"""
    api = (
        lark.Client.builder()
        .app_id(settings.FEISHU_APP_ID)
        .app_secret(settings.FEISHU_APP_SECRET.get_secret_value())
        .timeout(15)
        .log_level(lark.LogLevel.WARNING)
        .build()
    )

    def send_reply(message_id: str, text: str) -> None:
        # 官方消息 API：按原消息 ID 回复；稳定 uuid 帮助平台识别重复发送请求。
        body = (
            im.ReplyMessageRequestBody.builder()
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .uuid(str(uuid5(NAMESPACE_URL, f"{AGENT_ID}:reply:{message_id}")))
            .build()
        )
        request = im.ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
        response = api.im.v1.message.reply(request)
        if not response.success():
            raise RuntimeError(f"飞书消息 API 返回错误码 {response.code}")

    def send_card(
        receive_type: str, receive_id: str, card: dict[str, Any], event_id: str
    ) -> tuple[str, str]:
        body = (
            im.CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type("interactive")
            .content(json.dumps(card, ensure_ascii=False))
            .uuid(str(uuid5(NAMESPACE_URL, f"{settings.FEISHU_APP_ID}:{AGENT_ID}:card:{event_id}")))
            .build()
        )
        request = (
            im.CreateMessageRequest.builder()
            .receive_id_type(receive_type)
            .request_body(body)
            .build()
        )
        response = api.im.v1.message.create(request)
        if not response.success():
            raise RuntimeError(f"飞书卡片发送失败，错误码 {response.code}")
        if not response.data or not response.data.message_id or not response.data.chat_id:
            raise RuntimeError("飞书卡片响应缺少消息或聊天编号")
        return response.data.message_id, response.data.chat_id

    def reply_card(message_id: str, card: dict[str, Any]) -> tuple[str, str]:
        body = (
            im.ReplyMessageRequestBody.builder()
            .msg_type("interactive")
            .content(json.dumps(card, ensure_ascii=False))
            .uuid(
                str(
                    uuid5(NAMESPACE_URL, f"{settings.FEISHU_APP_ID}:{AGENT_ID}:answer:{message_id}")
                )
            )
            .build()
        )
        response = api.im.v1.message.reply(
            im.ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
        )
        if not response.success():
            raise RuntimeError(f"飞书回答卡片发送失败，错误码 {response.code}")
        if not response.data or not response.data.message_id or not response.data.chat_id:
            raise RuntimeError("回答卡片响应缺少消息或聊天编号")
        return response.data.message_id, response.data.chat_id

    def update_card(message_id: str, card: dict[str, Any]) -> None:
        body = (
            im.PatchMessageRequestBody.builder()
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        )
        response = api.im.v1.message.patch(
            im.PatchMessageRequest.builder().message_id(message_id).request_body(body).build()
        )
        if not response.success():
            raise RuntimeError(f"飞书卡片更新失败，错误码 {response.code}")

    sessions = FeishuSessionStore(settings.FEISHU_SESSION_DB_PATH, settings.FEISHU_APP_ID, AGENT_ID)
    session_ui = FeishuSessionUI(sessions, send_card, update_card)
    feedback_ui = None
    if settings.FEISHU_REPLY_MODE == "agent":
        feedback_store = FeedbackStore(
            settings.FEISHU_FEEDBACK_DB_PATH, settings.FEISHU_APP_ID, AGENT_ID
        )
        feedback_ui = FeishuFeedbackUI(feedback_store, reply_card, update_card, send_card)

    def record_invocation(message: FeishuMessage, thread_id: str, user_id: str) -> None:
        if handler.tasks:
            handler.tasks.store.record_invocation(message.message_id, thread_id, user_id)

    def record_result(message: FeishuMessage, run_id: str) -> None:
        if handler.tasks:
            handler.tasks.store.record_agent_result(message.message_id, run_id)

    generate_reply = (
        create_agent_reply(settings, sessions, feedback_ui.store, record_invocation, record_result)
        if feedback_ui
        else None
    )
    evaluation_ui = None
    if settings.FEISHU_EVAL_ENABLED and settings.FEISHU_REPLY_MODE == "agent":
        jobs = EvaluationJobStore(settings.FEISHU_EVAL_DB_PATH, settings.FEISHU_APP_ID, AGENT_ID)
        publisher = FeishuReportPublisher(
            FeishuHTTPTransport(settings),
            settings.FEISHU_APP_ID,
            settings.FEISHU_APP_SECRET.get_secret_value(),
            settings.FEISHU_REPORT_FOLDER_TOKEN,
        )

        def evaluate(job, stop):
            return asyncio.run(
                evaluate_job(
                    job,
                    stop,
                    str(settings.FEISHU_AGENT_URL).rstrip("/"),
                    settings.AUTH_SECRET.get_secret_value() if settings.AUTH_SECRET else None,
                    settings.FEISHU_EVAL_MODEL,
                    settings.FEISHU_EVAL_TIMEOUT,
                )
            )

        def publish(job, stop):
            def created(document_id: str) -> None:
                jobs.update(job.job_id, document_id=document_id)

            return publisher.publish(
                Path(job.output_dir) / "report.md",
                f"SupportPilot 评测 {job.job_id[:8]}",
                job.owner_open_id,
                created,
                stop,
            )

        def notify(job):
            send_card(
                "open_id",
                job.owner_open_id,
                evaluation_card(job),
                f"evaluation-complete:{job.job_id}:{job.status}",
            )

        worker = EvaluationWorker(jobs, evaluate, publish, notify)
        evaluation_ui = FeishuEvaluationUI(
            jobs,
            worker,
            settings.FEISHU_EVAL_CASES_PATH,
            settings.FEISHU_EVAL_OUTPUT_ROOT,
            send_card,
            set(
                filter(
                    None,
                    (item.strip() for item in settings.FEISHU_EVAL_ALLOWED_OPEN_IDS.split(",")),
                )
            ),
        )
    handler = FeishuHandler(send_reply, generate_reply, session_ui, feedback_ui, evaluation_ui)
    if settings.FEISHU_QUEUE_BACKEND == "redis":
        handler.enable_redis(settings)
    logger.info("飞书任务队列：%s", settings.FEISHU_QUEUE_BACKEND)
    logger.info("飞书回复模式：%s", settings.FEISHU_REPLY_MODE)
    events = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(
            lambda data: handler.on_event(json.loads(lark.JSON.marshal(data)))
        )
        .register_p2_application_bot_menu_v6(
            lambda data: handler.on_menu_event(json.loads(lark.JSON.marshal(data)))
        )
        .register_p2_card_action_trigger(
            lambda data: import_module(
                "lark_oapi.event.callback.model.p2_card_action_trigger"
            ).P2CardActionTriggerResponse(
                handler.on_card_action(json.loads(lark.JSON.marshal(data)))
            )
        )
        .build()
    )
    connection = lark.ws.Client(
        settings.FEISHU_APP_ID,
        settings.FEISHU_APP_SECRET.get_secret_value(),
        event_handler=events,
        log_level=lark.LogLevel.INFO,
    )
    handler_started = evaluation_started = False
    stop_metrics = None
    try:
        if evaluation_ui:
            evaluation_ui.worker.start()
            evaluation_started = True
        handler.start()
        handler_started = True
        if settings.METRICS_ENABLED:
            if handler.tasks is None:
                raise RuntimeError("飞书监控需要 FEISHU_QUEUE_BACKEND=redis")
            from observability.tasks import start_task_metrics

            stop_metrics = start_task_metrics(
                handler.tasks, settings.FEISHU_METRICS_HOST, settings.FEISHU_METRICS_PORT
            )
        logger.info("正在建立飞书长连接，连接成功后再到后台保存事件订阅。")
        connection.start()
    finally:
        try:
            if stop_metrics:
                stop_metrics()
            if handler_started:
                handler.stop()
        finally:
            if evaluation_ui and evaluation_started:
                evaluation_ui.worker.stop()
            # SDK 1.7.5 没有公开 stop；关闭连接前禁用重连，随后由 main 清理任务。
            connection._auto_reconnect = False
            asyncio.get_event_loop().run_until_complete(connection._disconnect())


if __name__ == "__main__":
    main()
