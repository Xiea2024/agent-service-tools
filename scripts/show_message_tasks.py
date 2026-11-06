"""Filter tasks, view timelines and perform audited, guarded recovery operations."""

import argparse
import getpass
import json
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from feishu_tasks import MessageTaskStore  # noqa: E402
from run_feishu import AGENT_ID, FeishuSettings  # noqa: E402


def local_time(value):
    return (
        datetime.fromisoformat(value)
        .astimezone(timezone(timedelta(hours=8)))
        .isoformat(timespec="seconds")
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="查询任务与事件流水；恢复前停止飞书进程")
    parser.add_argument("--task-id")
    parser.add_argument(
        "--latest", action="store_true", help="只显示最新普通问答（可配合筛选条件）"
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--since", help="任务创建时间起点，含；无时区按北京时间")
    parser.add_argument("--until", help="任务创建时间终点，不含")
    parser.add_argument("--state")
    parser.add_argument("--kind")
    parser.add_argument("--message-id", help="原始用户消息编号")
    parser.add_argument("--timeline", action="store_true")
    parser.add_argument("--event-limit", type=int, default=200)
    parser.add_argument("--after-event-id", type=int, default=0)
    parser.add_argument("--format", choices=["json", "table"], default="json")
    parser.add_argument(
        "--preview", action="store_true", help="显示问题前 80 字，可能包含私密信息，仅本机使用"
    )
    parser.add_argument("--action", choices=["abandon", "retry-send"])
    parser.add_argument("--backend-checked", action="store_true")
    parser.add_argument("--reason", help="恢复原因；勿填写密钥或私人正文")
    parser.add_argument("--actor", default=getpass.getuser(), help="操作人标签，默认本机账户")
    parser.add_argument("--operation-id", help="重复执行同一恢复请求时复用该编号")
    args = parser.parse_args(argv)
    if args.task_id and any(
        (args.latest, args.since, args.until, args.state, args.kind, args.message_id)
    ):
        parser.error("--task-id 不与列表筛选条件混用")
    if args.timeline and not (args.task_id or args.latest):
        parser.error("查看时间线需要 --task-id 或 --latest")
    if args.action and (not args.task_id or not args.reason or not args.reason.strip()):
        parser.error("恢复操作需要 --task-id 和非空 --reason")
    if not args.action and any((args.reason, args.operation_id, args.backend_checked)):
        parser.error("恢复参数只能与 --action 一起使用")
    if not 1 <= args.limit <= 1000 or not 1 <= args.event_limit <= 1000 or args.after_event_id < 0:
        parser.error("数量应为 1–1000，事件游标不能为负")
    settings = FeishuSettings()
    if not settings.FEISHU_TASK_DB_PATH.is_file():
        parser.error("消息任务数据库尚不存在")
    store = MessageTaskStore(
        settings.FEISHU_TASK_DB_PATH, json.dumps([settings.FEISHU_APP_ID, AGENT_ID])
    )
    operation_id = args.operation_id or str(uuid4())
    try:
        if args.action:
            store.recover(
                args.task_id,
                args.action,
                actor=args.actor,
                reason=args.reason,
                operation_id=operation_id,
                backend_checked=args.backend_checked,
            )
        tasks = (
            [store.get(args.task_id)]
            if args.task_id
            else store.recent(
                limit=1 if args.latest else args.limit,
                since=args.since,
                until=args.until,
                state=args.state,
                kind=args.kind or ("FeishuMessage" if args.latest else None),
                message_id=args.message_id,
            )
        )
        output = []
        for task in tasks:
            record = store.get_progress(task.task_id)
            item = {
                "task_id": task.task_id,
                "source_message_id": task.payload.get("message_id"),
                "created_at": task.created_at,
                "updated_at": task.updated_at,
                "kind": task.kind,
                "state": task.state,
                "attempts": task.attempts,
                "error": task.error,
                "snapshot_id": (task.reply or {}).get("snapshot_id"),
                "context": task.context,
                "notification": asdict(record) if record else None,
            }
            if args.preview:
                item["question_preview"] = " ".join(
                    "".join(
                        c for c in task.payload.get("text", "") if c.isprintable() or c.isspace()
                    ).split()
                )[:80]
            if args.action:
                item["operation_id"] = operation_id
            if args.timeline:
                events = store.timeline(
                    task.task_id, after_event_id=args.after_event_id, limit=args.event_limit
                )
                item["events"] = events
                item["next_after_event_id"] = (
                    events[-1]["event_id"] if len(events) == args.event_limit else None
                )
            output.append(item)
    except (ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    if args.format == "json":
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return
    if not output:
        print("没有匹配的任务")
    for item in output:
        print(f"{local_time(item['created_at'])}  {item['state']}  {item['kind']}")
        print(f"task_id={item['task_id']}")
        print(
            f"message_id={item['source_message_id']}  trace_id={item['context'].get('trace_id', '')}"
        )
        if args.preview:
            print(f"问题摘要：{item['question_preview']}")
        if args.action:
            print(f"operation_id={operation_id}")
        for event in item.get("events", []):
            print(
                f"  #{event['event_id']} {local_time(event['occurred_at'])} {event['event_type']} "
                f"{event['from_state'] or '-'} → {event['to_state'] or '-'} "
                f"attempt={event['attempt']} error={event['error_code'] or '-'}"
            )
            print("    " + json.dumps(event["details"], ensure_ascii=False))
            if event["actor"]:
                print(f"    操作人={event['actor']} 原因={event['reason']}")
        if item.get("next_after_event_id"):
            print(f"后续事件：--after-event-id {item['next_after_event_id']}")


if __name__ == "__main__":
    main()
