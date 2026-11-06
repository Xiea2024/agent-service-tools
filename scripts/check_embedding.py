"""Embedding 连通验证入口：向硅基流动发送一句短文本，检查返回向量。

使用独立的向量模型配置，展示实际维度和少量样本值，不输出密钥或完整向量。
不读取飞书正文、不启动 Agent，也不创建 Qdrant Collection。
"""

import argparse
import math
import sys
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openai import (  # noqa: E402
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
)
from pydantic import ValidationError  # noqa: E402

from embedding_client import EmbeddingSettings, create_embeddings  # noqa: E402


def main() -> None:
    """只做一条查询向量验证，配置或 API 失败时返回非零退出码。"""
    parser = argparse.ArgumentParser(description="验证硅基流动 Embedding 接口和实际向量维度")
    parser.add_argument(
        "--text", default="订单接口在部署新版本后返回 500。", help="用于验证的短文本"
    )
    args = parser.parse_args()
    if not args.text.strip():
        parser.error("测试文本不能为空")
    try:
        settings = EmbeddingSettings()
    except ValidationError as exc:
        fields = ", ".join(str(error["loc"][0]) for error in exc.errors(include_input=False))
        parser.exit(1, f"请在根目录 .env 配置：{fields}；密钥仅保存在本机。\n")
    started = perf_counter()
    try:
        vector = create_embeddings(settings).embed_query(args.text)
    except APITimeoutError:
        parser.exit(1, "Embedding 请求超时，请检查网络或稍后再试。\n")
    except APIConnectionError:
        parser.exit(1, "Embedding 连接失败，请检查接口地址、网络、代理与证书。\n")
    except APIStatusError as exc:
        # 只输出状态码，避免 SDK 的原始响应正文意外带出敏感配置。
        parser.exit(
            1, f"Embedding 请求失败：HTTP {exc.status_code}；请检查密钥、模型权限或限流状态。\n"
        )
    except (APIResponseValidationError, ValueError, KeyError, IndexError, TypeError):
        parser.exit(1, "Embedding 响应格式异常，无法读取有效向量。\n")
    if not vector or any(
        not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value)
        for value in vector
    ):
        parser.exit(1, "Embedding 未返回有效的有限数值向量。\n")
    print(f"模型：{settings.EMBEDDING_MODEL}\n文本字符数：{len(args.text)}")
    print(f"实际向量维度：{len(vector)}\n前 5 个数值：{vector[:5]}")
    print(f"耗时：{perf_counter() - started:.2f} 秒\nEmbedding 连通验证成功。")


if __name__ == "__main__":
    main()
