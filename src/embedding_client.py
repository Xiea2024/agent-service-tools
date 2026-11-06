"""独立 Embedding 接入层：使用硅基流动的 OpenAI 兼容接口生成文本向量。

复用已安装的 LangChain OpenAIEmbeddings，供后续文档入库与问题检索共同使用。
配置独立于聊天模型和飞书凭据，本模块不读取文档、不切分文本或写入向量库。
"""

import ssl
from pathlib import Path

import httpx
from langchain_openai import OpenAIEmbeddings
from openai import DefaultHttpxClient
from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class EmbeddingSettings(BaseSettings):
    """从根目录 .env 读取向量模型配置，环境变量可覆盖本机文件。"""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent / ".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )
    EMBEDDING_API_KEY: SecretStr = Field(min_length=1)
    EMBEDDING_BASE_URL: AnyHttpUrl = AnyHttpUrl("https://api.siliconflow.cn/v1")
    EMBEDDING_MODEL: str = Field(default="BAAI/bge-m3", min_length=1)
    EMBEDDING_TIMEOUT: float = Field(default=30, gt=0, le=120)
    EMBEDDING_MAX_RETRIES: int = Field(default=1, ge=0, le=3)
    EMBEDDING_FORCE_TLS12: bool = False


def create_embedding_http_client(config: EmbeddingSettings) -> httpx.Client:
    """同步 HTTP 连接复用 SDK 默认值；按需兼容 TLS 1.2，始终保留证书与主机名校验。

    当前 Agent 通过 to_thread 调用同步 Embedding，故这里配置的是同步传输。
    调用方显式关闭连接，避免每次检索后遗留连接池。
    """
    context = ssl.create_default_context()
    if config.EMBEDDING_FORCE_TLS12:
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.maximum_version = ssl.TLSVersion.TLSv1_2
    return DefaultHttpxClient(verify=context)


def create_embeddings(
    settings: EmbeddingSettings | None = None, *, http_client: httpx.Client | None = None
) -> OpenAIEmbeddings:
    """统一创建文档/查询向量客户端；仅构建客户端，不会立即发出请求。"""
    config = settings if settings is not None else EmbeddingSettings()
    if http_client is None and config.EMBEDDING_FORCE_TLS12:
        # 入库/连通检查也复用同一兼容选项；显式传入的 MockTransport 保持调用方控制。
        http_client = create_embedding_http_client(config)
    return OpenAIEmbeddings(
        api_key=config.EMBEDDING_API_KEY,
        base_url=str(config.EMBEDDING_BASE_URL).rstrip("/"),
        model=config.EMBEDDING_MODEL,
        timeout=config.EMBEDDING_TIMEOUT,
        max_retries=config.EMBEDDING_MAX_RETRIES,
        http_client=http_client,
        # 国产兼容服务使用自己的 tokenizer，直接发送字符串，避免 OpenAI token ID 不兼容。
        check_embedding_ctx_length=False,
        # 明确要求浮点数组，便于验证与后续向量入库；本轮不指定或缩减模型维度。
        model_kwargs={"encoding_format": "float"},
    )
