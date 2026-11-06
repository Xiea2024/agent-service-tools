"""RAG 入库命令：chunks JSON → Embedding → 可重用向量文件 → Qdrant。

入口只编排已有模块，不启动 Agent、飞书长连接或聊天模型。
默认仅打印数量和维度，不输出正文、完整向量或密钥。
"""

import argparse
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openai import APIError  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from qdrant_client.http.exceptions import (  # noqa: E402
    ResponseHandlingException,
    UnexpectedResponse,
)

from document_vectors import ChunkBatch, DocumentVectors, embed_chunks  # noqa: E402
from embedding_client import EmbeddingSettings, create_embeddings  # noqa: E402
from qdrant_store import QdrantSettings, create_qdrant_client, index_vectors  # noqa: E402


def main() -> None:
    """先把结果保存到本地，再入库；数据库失败不会丢失已付费生成的向量。"""
    parser = argparse.ArgumentParser(description="为飞书文档片段生成向量并写入 Qdrant")
    parser.add_argument("input", type=Path, help="*.chunks.json；重用模式下为 *.vectors.json")
    parser.add_argument("--output", type=Path, help="向量文件路径，默认输入同目录 *.vectors.json")
    parser.add_argument(
        "--reuse-vectors", action="store_true", help="直接导入向量文件，不请求 Embedding"
    )
    parser.add_argument("--batch-size", type=int, default=16, help="Embedding 每批片段数，1–32")
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 32:
        parser.error("--batch-size 应在 1–32 之间")
    if args.reuse_vectors and args.output:
        parser.error("重用模式不生成新的向量文件，请省略 --output")
    output = args.output or args.input.with_name(
        args.input.stem.removesuffix(".chunks") + ".vectors.json"
    )
    if not args.reuse_vectors and output.resolve() == args.input.resolve():
        parser.error("输出不能覆盖输入片段文件")

    stage = "读取与配置"
    try:
        settings = QdrantSettings()
        raw = args.input.read_text(encoding="utf-8")
        if args.reuse_vectors:
            data = DocumentVectors.model_validate_json(raw)
            print("重用已有向量：本次不调用 Embedding。", flush=True)
        else:
            batch = ChunkBatch.model_validate_json(raw)
            config = EmbeddingSettings()
            # 数据库不可达时先停止，避免明知无法入库仍产生 Embedding 开销。
            stage = "Qdrant 连通检查"
            with closing(create_qdrant_client(settings)) as client:
                client.get_collections()
            stage = "Embedding"
            print(f"正在为 {len(batch.chunks)} 个片段生成文档向量……", flush=True)
            data = embed_chunks(
                batch, create_embeddings(config), config, batch_size=args.batch_size
            )
            stage = "保存向量文件"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(data.model_dump_json(indent=2), encoding="utf-8")
            print(f"向量文件已保存：{output.resolve()}", flush=True)
        stage = "Qdrant 入库与读回验证"
        with closing(create_qdrant_client(settings)) as client:
            count = index_vectors(client, settings.QDRANT_COLLECTION, data)
        print(f"入库并读回验证成功：{count} 个片段，{data.dimension} 维，Cosine。")
        print(f"Collection：{settings.QDRANT_COLLECTION}")
    except ValidationError as exc:
        # Pydantic 默认错误可能带原始输入，故只打印字段位置，不泄露正文或配置值。
        fields = ", ".join(".".join(map(str, e["loc"])) or "文件整体" for e in exc.errors())
        parser.exit(1, f"{stage}失败：数据或配置校验不通过（{fields}）。\n")
    except APIError as exc:
        parser.exit(1, f"Embedding 请求失败：{type(exc).__name__}，请检查密钥、余额或网络。\n")
    except (ResponseHandlingException, UnexpectedResponse) as exc:
        parser.exit(1, f"{stage}失败：{type(exc).__name__}，请检查 Qdrant 地址、服务或认证。\n")
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"{stage}失败：{exc}\n")


if __name__ == "__main__":
    main()
