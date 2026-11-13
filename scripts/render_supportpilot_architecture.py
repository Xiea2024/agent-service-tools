"""Render the code-verified SupportPilot architecture as editable SVG and PNG.

Run with the project Python; only Pillow is required. No network or secrets used.
"""

import math
from html import escape
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
W, H = 2560, 1990
im = Image.new("RGB", (W, H), "#f5f7fb")
d = ImageDraw.Draw(im)
svg = [
    f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
    '<rect width="100%" height="100%" fill="#f5f7fb"/>',
]
INK = "#172b4d"
MUTED = "#526581"
BLUE = "#2864dc"
TEAL = "#078779"
PURPLE = "#7851bc"


def text(x, y, s, size=25, color=INK, bold=False):
    font = ImageFont.truetype(
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc", size
    )
    d.text((x, y), s, font=font, fill=color)
    svg.append(
        f'<text x="{x}" y="{y}" dominant-baseline="text-before-edge" font-family="Microsoft YaHei, Noto Sans CJK SC, sans-serif" font-size="{size}" font-weight="{700 if bold else 400}" fill="{color}">{escape(s)}</text>'
    )


def box(x, y, w, h, fill="#ffffff", stroke="#d9e1ef", radius=18):
    d.rounded_rectangle((x, y, x + w, y + h), radius, fill, stroke, 2)
    svg.append(
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
    )


def node(x, y, w, h, title, lines=(), fill="#ffffff", accent=BLUE, size=24):
    box(x, y, w, h, fill)
    text(x + 20, y + 16, title, 28, accent, True)
    for i, line in enumerate(lines):
        text(x + 20, y + 60 + i * 35, line, size)


def arrow(points, color=BLUE, dash=False):
    for a, b in zip(points, points[1:]):
        if dash:
            length = math.dist(a, b)
            for i in range(0, int(length), 16):
                p = tuple(a[j] + (b[j] - a[j]) * i / length for j in (0, 1))
                q = tuple(a[j] + (b[j] - a[j]) * min(i + 8, length) / length for j in (0, 1))
                d.line((p, q), fill=color, width=3)
        else:
            d.line((a, b), fill=color, width=4)
    a, b = points[-2:]
    angle = math.atan2(b[1] - a[1], b[0] - a[0])
    tri = [
        b,
        (b[0] - 15 * math.cos(angle - 0.45), b[1] - 15 * math.sin(angle - 0.45)),
        (b[0] - 15 * math.cos(angle + 0.45), b[1] - 15 * math.sin(angle + 0.45)),
    ]
    d.polygon(tri, fill=color)
    coords = " ".join(f"{x},{y}" for x, y in points)
    svg.append(
        f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="4"'
        + (' stroke-dasharray="8 8"' if dash else "")
        + "/>"
    )
    svg.append(f'<polygon points="{" ".join(f"{x},{y}" for x, y in tri)}" fill="{color}"/>')


text(60, 35, "SupportPilot · 飞书研发问题处理助手", 53, bold=True)
text(62, 108, "当前实现架构  /  普通问答 · 会话与恢复 · 文档 RAG · 评测报告", 28, MUTED)
box(1950, 47, 550, 65, "#e6f4ef", "#c9e8da")
text(1972, 63, "本地 Python + Docker 数据服务", 27, TEAL, True)

# Main online path, with real process boundaries.
box(60, 185, 410, 835, "#edf4ff", "#cbdcf8")
text(85, 208, "01  飞书用户与平台", 31, BLUE, True)
node(
    85,
    280,
    360,
    175,
    "用户单聊",
    ["研发问题 / 多轮补充", "菜单与命令：会话、评测", "卡片操作：回答评价"],
    size=23,
)
node(85, 535, 360, 145, "飞书开放平台", ["SDK WebSocket 长连接", "消息 / 菜单 / 卡片事件"], size=23)
arrow([(265, 455), (265, 535)])
node(
    85,
    785,
    360,
    190,
    "用户看到的结果",
    ["接收回执 → 处理中 → 答案", "带来源链接的回答卡片", "评价结果 / 评测报告链接"],
    accent=TEAL,
    size=22,
)

box(520, 185, 960, 835, "#eff8f5", "#cbe6dc")
text(545, 208, "02  飞书桥接进程  ·  run_feishu.py", 31, TEAL, True)
node(
    550,
    280,
    340,
    160,
    "事件入口 / 路由",
    ["仅处理用户单聊", "识别消息、菜单与反馈"],
    accent=TEAL,
    size=23,
)
node(
    940,
    280,
    510,
    160,
    "持久任务队列",
    ["先写 SQLite 台账 → Redis（Docker）", "Streams / 消费组 / 去重 / 未确认记录"],
    accent=TEAL,
    size=23,
)
arrow([(445, 600), (495, 600), (495, 360), (550, 360)])
text(475, 520, "事件", 21, BLUE)
arrow([(890, 350), (940, 350)], TEAL)
node(
    550,
    475,
    340,
    140,
    "多轮会话目录",
    ["用户 / 单聊 → 当前 thread_id", "新建、列表、切换会话"],
    accent=TEAL,
    size=21,
)
node(
    940,
    475,
    510,
    140,
    "单个问答 Worker 线程",
    ["按台账顺序执行 / 本机文件锁", "AgentClient.invoke + 当前会话"],
    accent=TEAL,
    size=23,
)
arrow([(1195, 440), (1195, 475)], TEAL)
arrow([(890, 540), (940, 540)], TEAL)
node(
    550,
    660,
    340,
    145,
    "回答快照与反馈",
    ["保存问题、答案、run_id", "校验归属 / 首次有效评价"],
    accent=TEAL,
    size=22,
)
node(
    940,
    660,
    510,
    145,
    "独立回复通知线程",
    ["更新同一张进度卡片 → 最终答案", "发送失败重试 / 已有快照复用"],
    accent=TEAL,
    size=23,
)
arrow([(1010, 615), (1010, 635), (720, 635), (720, 660)], TEAL)
arrow([(890, 730), (940, 730)], TEAL)
arrow([(940, 780), (915, 780), (915, 845), (445, 845)], TEAL)
text(550, 815, "飞书消息 API：发送 / 更新卡片", 21, TEAL)
node(
    550,
    875,
    900,
    110,
    "SQLite 分库保存业务状态",
    ["feishu-tasks  ·  feishu-sessions  ·  feishu-feedback  ·  evaluation-jobs"],
    fill="#e3f1eb",
    accent=TEAL,
    size=22,
)

box(1540, 185, 960, 835, "#f1effa", "#ddd5ef")
text(1565, 208, "03  FastAPI 后端进程  ·  run_service.py", 31, PURPLE, True)
node(
    1570,
    280,
    900,
    130,
    "Agent API  /  dev-issue-agent",
    [
        "/invoke 返回最终回答；/stream 输出完整消息流（供评测）",
        "RunnableConfig：thread_id / user_id / run_id",
    ],
    accent=PURPLE,
    size=23,
)
arrow([(1450, 505), (1507, 505), (1507, 345), (1570, 345)])
text(1470, 424, "HTTP", 20, BLUE)
node(
    1570,
    465,
    550,
    160,
    "LangGraph · 单 Agent 工具循环",
    [
        "START → prepare → triage → END",
        "有 tool_calls：tools → triage",
        "每轮最多 2 次检索 / 总预算 65 秒",
    ],
    accent=PURPLE,
    size=22,
)
node(
    2190,
    465,
    280,
    160,
    "聊天模型 API",
    ["读取历史与证据", "追问 / 分析 / 回答"],
    accent=PURPLE,
    size=22,
)
arrow([(1845, 410), (1845, 465)], PURPLE)
arrow([(2120, 530), (2190, 530)], PURPLE)
arrow([(2190, 575), (2120, 575)], PURPLE)
node(
    1570,
    685,
    550,
    120,
    "工具：search_feishu_documents",
    ["查询向量 → Top-K + 阈值 → 正文与出处"],
    accent=PURPLE,
    size=21,
)
node(2190, 685, 280, 120, "Embedding API", ["用户问题 → 查询向量"], accent=PURPLE, size=21)
arrow([(1845, 625), (1845, 685)], PURPLE)
arrow([(2050, 685), (2050, 650), (2090, 650), (2090, 625)], PURPLE)
arrow([(2120, 745), (2190, 745)], PURPLE)
node(
    1570,
    865,
    550,
    120,
    "LangGraph checkpoint · SQLite",
    ["checkpoints.db：线程消息与图状态"],
    accent=PURPLE,
    size=23,
)
node(2190, 865, 280, 120, "Qdrant · Docker", ["向量 + 正文 + 来源"], accent=PURPLE, size=21)
arrow([(2160, 745), (2160, 825), (2330, 825), (2330, 865)], PURPLE)
arrow([(1590, 625), (1555, 625), (1555, 925), (1570, 925)], PURPLE, True)
text(1570, 826, "检索结果返回模型，最终答案沿 HTTP 返回 Worker", 21, MUTED)

# Supporting workflows separated from the online question path.
box(60, 1060, 1190, 460)
text(85, 1085, "04  知识准备链路  ·  手动导入，按需执行", 31, TEAL, True)
node(90, 1160, 330, 125, "授权飞书 wiki / docx", ["read_feishu_document.py"], accent=TEAL, size=21)
node(485, 1160, 330, 125, "读取正文 → 字符切分", ["chunk_feishu_document.py"], accent=TEAL, size=21)
node(880, 1160, 335, 125, "文档 Embedding", ["生成片段向量"], accent=TEAL, size=23)
arrow([(420, 1220), (485, 1220)], TEAL)
arrow([(815, 1220), (880, 1220)], TEAL)
node(
    90,
    1340,
    1125,
    130,
    "index_document_chunks.py → Qdrant 文档索引",
    [
        "保存 chunk_id、document_id、title、source_url、content 与向量",
        "在线检索使用同一 Embedding 配置；默认 Top-K=3，分数阈值=0.45",
    ],
    accent=TEAL,
    size=24,
)
arrow([(1045, 1285), (1045, 1340)], TEAL)

box(1290, 1060, 1210, 460)
text(1315, 1085, "05  回归评测与飞书报告  ·  独立评测线程", 31, PURPLE, True)
node(
    1320, 1160, 345, 125, "菜单 /eval 或 CLI", ["任务快照 / job_id / 状态"], accent=PURPLE, size=22
)
node(
    1720,
    1160,
    750,
    125,
    "共享评测器 → AgentClient.astream → /stream",
    ["同一 dev-issue-agent；固定 12 案例、14 轮"],
    accent=PURPLE,
    size=24,
)
arrow([(1665, 1220), (1720, 1220)], PURPLE)
node(
    1320,
    1340,
    530,
    130,
    "本地评测产物",
    ["完整消息 / 检索证据 / 规则结果 / 耗时", "JSONL + JSON + Markdown"],
    accent=PURPLE,
    size=22,
)
node(
    1910,
    1340,
    560,
    130,
    "飞书报告发布",
    ["写入文档 → 授予并核对阅读权限", "获取访问链接 → 发送完成卡片"],
    accent=PURPLE,
    size=23,
)
arrow([(2095, 1285), (2095, 1310), (1585, 1310), (1585, 1340)], PURPLE)
arrow([(1850, 1405), (1910, 1405)], PURPLE)

box(60, 1560, 2440, 190, "#edf1f8")
text(85, 1580, "06  可观测性与关联标识", 30, BLUE, True)
text(
    85,
    1635,
    "Prometheus 抓取 FastAPI :8080/metrics 与飞书进程 :9101/metrics → Grafana 看板与告警",
    27,
)
text(
    85,
    1680,
    "可选 Langfuse 回调：Agent / 模型 / 工具追踪；task_id → thread_id → run_id → snapshot_id 串联任务与回答",
    25,
    MUTED,
)

text(65, 1790, "恢复语义", 27, bold=True)
text(
    250,
    1790,
    "queued → invoking → ready → succeeded；已有快照复用；调用结果不明且无快照 → needs_review",
    25,
)
text(65, 1840, "实现边界", 27, bold=True)
text(
    250,
    1840,
    "单 Agent、本机单问答 Worker；知识库手动导入；普通问答非逐字流式；评测发布失败保留本地产物",
    25,
)
text(
    65,
    1907,
    "依据：当前仓库实现与配置（Redis + agent + metrics）；图示不代表各外部服务已经在线验收。",
    23,
    MUTED,
)

svg.append("</svg>")
out = ROOT / "media"
out.mkdir(exist_ok=True)
im.save(out / "supportpilot_architecture.png")
(out / "supportpilot_architecture.svg").write_text("\n".join(svg), encoding="utf-8")
print("Generated media/supportpilot_architecture.png and .svg")
