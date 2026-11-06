"""Evaluate SupportPilot through the existing HTTP client; no QA logic lives here."""

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from client import AgentClient, AgentClientError  # noqa: E402
from evaluation import load_suite, run_evaluation, timestamp  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="运行固定案例，生成 SupportPilot 本地评测报告")
    parser.add_argument("--cases", type=Path, default=ROOT / "evals/supportpilot/cases.json")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--model", help="服务支持的模型枚举名称；省略则使用服务默认模型")
    parser.add_argument("--timeout", type=float, default=90, help="每轮总超时秒数，默认 90")
    parser.add_argument(
        "--case", action="append", dest="case_ids", help="仅运行指定 ID，可重复传入"
    )
    parser.add_argument("--output-dir", type=Path, help="新的结果目录；已存在时拒绝覆盖")
    parser.add_argument("--validate-only", action="store_true", help="仅校验并列出案例，不调用后端")
    args = parser.parse_args()
    if not 0 < args.timeout < float("inf"):
        parser.error("timeout 必须为有限正数")
    try:
        suite = load_suite(args.cases, args.case_ids)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.validate_only:
        for case in suite.cases:
            print(f"{case.id} | {case.title} | {len(case.turns)} 轮")
        print(f"共 {len(suite.cases)} 个案例，{sum(len(c.turns) for c in suite.cases)} 轮")
        return

    load_dotenv(ROOT / ".env")
    try:
        client = AgentClient(
            base_url=args.base_url.rstrip("/"),
            agent="dev-issue-agent",
            timeout=min(args.timeout, 5),
        )
        if args.model and client.info and args.model not in client.info.models:
            raise AgentClientError(f"服务未提供模型：{args.model}")
        client.timeout = args.timeout
    except (AgentClientError, ValueError) as exc:
        parser.exit(
            2,
            f"评测未开始：后端连接或配置检查未通过。\n"
            f"后端地址：{args.base_url}\n原因：{exc}\n"
            "请在另一个终端启动后端，并保持运行：\n"
            ".\\.venv\\Scripts\\python.exe src/run_service.py\n"
            "若后端已启动，请检查 --base-url、AUTH_SECRET 和服务提供的 Agent/模型。\n",
        )
    output_dir = args.output_dir or (
        ROOT
        / "evals/supportpilot/runs"
        / f"{datetime.fromisoformat(timestamp()):%Y%m%d-%H%M%S}-{uuid4().hex[:8]}"
    )
    try:
        result = asyncio.run(run_evaluation(suite, client, output_dir, args.model, args.timeout))
    except FileExistsError:
        parser.exit(2, f"结果目录已存在，拒绝覆盖：{output_dir}\n")
    except KeyboardInterrupt:
        parser.exit(130, f"评测已中断，已完成轮次保留在：{output_dir}\n")
    print(f"规则通过：{result['passed_cases']}/{result['total_cases']}；答案质量待人工复核")
    print(f"报告：{output_dir / 'report.md'}")
    if result["passed_cases"] != result["total_cases"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
