"""Supervisor Agent：唯一的职责是把一轮请求委派给一个专家。

它不做问答、不检索、不读写记忆，只输出一个委派决定。刻意用“工具调用”而不是
自然语言表达决定：`tool_calls` 是结构化的、可校验的，解析失败时能明确回退，
而不是从一段散文里猜意图。

两条硬约束：

1. **委派消息不写进 messages**。图的节点只返回 ``Command(goto=...)`` 和一个
   状态字段，不把这次带 tool_calls 的 AIMessage 塞进会话。否则它会出现在 SSE
   消息流里，让评测的 ``known_tools``（只允许 search_feishu_documents）和
   ``tool_budget``（≤2 次）把每一轮都判失败。
2. **拒绝的 target 一律回退**。模型给出白名单外的值、没调用工具、或者超时，
   都退回文档专家，并记录 ``decided_by``，让归因时能区分“模型选错”和“专家答错”。
"""

from __future__ import annotations

from typing import Any

from agents.route_rules import classify_intent

DELEGATE_TOOL_NAME = "delegate"
DOC_EXPERT = "doc_expert"
LOG_EXPERT = "log_expert"
EXPERTS: tuple[str, ...] = (DOC_EXPERT, LOG_EXPERT)

# OpenAI 兼容的函数声明形式：既能给真实 Provider，也能给测试用的假模型。
DELEGATE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": DELEGATE_TOOL_NAME,
        "description": "把本轮问题委派给一个专家，每轮只调用一次。",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "enum": list(EXPERTS),
                    "description": (
                        "doc_expert：查知识库/文档，回答排查步骤；"
                        "log_expert：用户粘贴了日志或堆栈，需要解释其中的事实"
                    ),
                }
            },
            "required": ["target"],
        },
    },
}

SUPERVISOR_PROMPT = """
你是研发问题处理助手的调度器。你的唯一任务是判断这一轮应该由哪个专家处理，
然后调用 delegate 工具，只调用一次。不要回答问题本身。

【doc_expert】
适用于：询问排查步骤、需要知识库或文档依据、描述现象但没贴日志、
要求生成问题报告或交接材料、寒暄与能力咨询。

【log_expert】
适用于：用户直接粘贴了一段日志、堆栈、traceback、异常行，需要解释其中
的事实（时间、级别、异常类型、request_id、状态码、堆栈位置）。

【判断规则】
1. 只要有堆栈、traceback、`File "...", line N`、`at xxx(Yyy.java:N)`、
   级别+时间戳之类**日志结构**，就选 log_expert。
2. 只是提到“返回 500”“报错了”但没有日志结构，选 doc_expert——那是现象描述，
   不是日志。
3. 拿不准时选 doc_expert：它能检索、能追问，比错误地进入日志分支更安全。
4. 不要根据用户的语言、长度或礼貌程度判断，只看内容属于哪一类。
"""


def parse_delegation(response: Any) -> str | None:
    """从模型响应里取出合法的委派目标；不合法的值一律当作没决定。"""
    calls = getattr(response, "tool_calls", None) or []
    for call in calls:
        if not isinstance(call, dict) or call.get("name") != DELEGATE_TOOL_NAME:
            continue
        args = call.get("args")
        target = args.get("target") if isinstance(args, dict) else None
        if isinstance(target, str) and target in EXPERTS:
            return target
    return None


def choose_by_rules(text: str) -> str:
    """模型没给出决定时的兜底：用确定性规则选一个，绝不把请求丢掉。"""
    return LOG_EXPERT if classify_intent(text).intent == "log_diag" else DOC_EXPERT
