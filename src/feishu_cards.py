"""飞书会话管理的展示层：构造原生 JSON 2.0 卡片与回传按钮。

只构造数据，不发送消息、不修改会话。按钮提交的线程编号还需 SQL 层验证归属，
普通用户标题作为纯文本按钮展示，避免被当成 Markdown 或链接解析。
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from feedback import MAX_COMMENT_LENGTH, AnswerFeedback, AnswerSnapshot
from feishu_sessions import FeishuSession


def notice_card(title: str, text: str) -> dict[str, Any]:
    """用于首次加载和失败反馈，保持与会话卡片一致的视觉结构。"""
    return {
        "schema": "2.0",
        # 首次加载后用 PATCH 更新同一张卡片，需要声明为共享可更新卡片。
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
        "body": {"elements": [{"tag": "markdown", "content": text}]},
    }


def progress_card(state: str, request_rejected: bool = False) -> dict[str, Any]:
    content = {
        "queued": ("已收到", "你的问题已保存，正在等待处理。"),
        "processing": ("正在处理", "正在分析你的问题，请稍候。"),
        "answered": ("回答已生成", "正在准备展示回答，请稍候。"),
        "needs_review": (
            "处理结果待确认",
            "本次调用的执行结果暂无法确认，未自动重新调用模型。请联系维护者核对任务记录。",
        ),
        "blocked": (
            "本条尚未处理",
            "此聊天的先前调用结果待确认，本条没有调用模型。请联系维护者处理先前任务。",
        ),
        "failed": ("暂未完成", "本次任务未能完成，请联系维护者查看任务记录。"),
    }
    title, text = content[state]
    card = notice_card(title, text)
    if state == "failed" and request_rejected:
        card = notice_card(
            "问答服务暂不可用", "暂时无法调用问答服务，本条未执行。请稍后重新提问，或联系维护者。"
        )
    if state in {"needs_review", "blocked", "failed"}:
        card["header"]["template"] = "orange"
    return card


def _button(label: str, action: str, chat_id: str, thread_id: str = "") -> dict[str, Any]:
    return {
        "tag": "button",
        "type": "default",
        "width": "fill",
        "text": {"tag": "plain_text", "content": label},
        "behaviors": [
            {
                "type": "callback",
                "value": {
                    "action": action,
                    "chat_id": chat_id,
                    "thread_id": thread_id,
                },
            }
        ],
    }


def session_card(session: FeishuSession, chat_id: str, title: str = "当前会话") -> dict[str, Any]:
    """展示新建/切换完成后的真实状态，不让模型生成“操作成功”的声明。"""
    created = datetime.fromisoformat(session.created_at).astimezone(timezone(timedelta(hours=8)))
    card = notice_card(
        title,
        f"**会话编号**：{session.thread_id[:8]}\n"
        f"**登记时间**：{created:%Y-%m-%d %H:%M:%S}（UTC+8）\n"
        f"**已获有效回复**：{session.turn_count} 轮\n\n"
        "接下来发送的消息将在此会话中继续。其他会话的历史仍然保留。",
    )
    card["header"]["subtitle"] = {"tag": "plain_text", "content": session.title}
    card["body"]["elements"].extend(
        [
            {
                "tag": "column_set",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [_button("新建会话", "session_new", chat_id)],
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [_button("历史会话", "session_history", chat_id)],
                    },
                ],
            },
        ]
    )
    return card


def history_card(
    sessions: list[FeishuSession], current_thread: str, chat_id: str
) -> dict[str, Any]:
    """最近会话以纯文本按钮展示，点击回传 thread_id 后继续已有上下文。"""
    card = notice_card("历史会话", "最近 8 个会话。点击条目继续，已标注当前会话。")
    card["body"]["elements"].extend(
        [
            _button(
                f"{'● 当前 · ' if item.thread_id == current_thread else '继续 · '}{item.title[:30]} · {item.thread_id[:8]}",
                "session_switch",
                chat_id,
                item.thread_id,
            )
            for item in sessions
        ]
    )
    card["body"]["elements"].append(_button("＋ 新建会话", "session_new", chat_id))
    return card


def answer_card(snapshot: AnswerSnapshot, feedback: AnswerFeedback | None = None) -> dict[str, Any]:
    card = notice_card("SupportPilot 回答", snapshot.answer)
    elements = card["body"]["elements"]
    elements.append({"tag": "hr"})
    if feedback:
        label = "有帮助" if feedback.rating == "helpful" else "没帮助"
        elements.append({"tag": "markdown", "content": f"**反馈已保存：{label}**。感谢你的评价！"})
        if feedback.comment:
            elements.append({"tag": "markdown", "content": "补充意见已一并保存。"})
        return card
    elements.append(
        {
            "tag": "markdown",
            "content": "这条回答对你有帮助吗？可先填写意见，再点击下方按钮提交。首次提交后不可修改。",
        }
    )
    buttons = []
    for rating, label in (("helpful", "有帮助"), ("not_helpful", "没帮助")):
        buttons.append(
            {
                "tag": "button",
                "name": f"submit_{rating}",
                "form_action_type": "submit",
                "type": "primary" if rating == "helpful" else "default",
                "text": {"tag": "plain_text", "content": label},
                "behaviors": [
                    {
                        "type": "callback",
                        "value": {
                            "action": "feedback_submit",
                            "snapshot_id": snapshot.snapshot_id,
                            "rating": rating,
                        },
                    }
                ],
            }
        )
    elements.append(
        {
            "tag": "form",
            "name": "answer_feedback",
            "elements": [
                {
                    "tag": "input",
                    "name": "comment",
                    "input_type": "multiline_text",
                    "max_length": MAX_COMMENT_LENGTH,
                    "required": False,
                    "placeholder": {
                        "tag": "plain_text",
                        "content": "选填意见（最多 1000 字），请勿填写密码或密钥",
                    },
                },
                *buttons,
            ],
        }
    )
    return card
