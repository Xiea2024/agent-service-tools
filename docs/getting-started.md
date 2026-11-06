# SupportPilot 启动与运维

本文对应当前代码，命令在项目根目录的 Windows PowerShell 执行。部署方式为本地 Python 应用、Docker Redis 和 Qdrant；Streamlit 可选。已有安装请保留 `.env`、checkpoint、`.local` 业务库和 Docker 数据卷，按需补配置。

## 1. 环境与依赖

- Python：项目声明 `>=3.12,<3.15`，本地开发使用 3.13；不把声明范围等同于每种环境都已实机验收。
- uv、Docker Desktop 与 Docker Compose 可用。
- 一个支持工具调用的聊天模型；RAG 另需 Embedding 服务。
- 飞书自建应用、机器人能力及允许交互的用户范围；知识文档需授予应用读取权限。

```powershell
python --version
uv --version
docker version
docker compose version
uvx --from uv==0.13.5 uv sync --frozen --group feishu --group rag
```

`feishu` 组包含飞书 SDK 与 Redis 客户端，`rag` 组包含 Qdrant 客户端。仅安装默认依赖不会安装这两个组。锁文件使用 `--frozen` 安装，不在启动时重新解析或升级依赖。

首次创建配置，已有 `.env` 时不会覆盖：

```powershell
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
```

## 2. 配置 `.env`

以下是 OpenAI 兼容聊天接口的示例。将占位内容换成自己的配置，并在 `.env` 现有行修改，避免重复定义同名变量。也可使用原框架支持的其他模型 Provider。

```dotenv
DEFAULT_MODEL=openai-compatible
COMPATIBLE_MODEL=你的模型标识
COMPATIBLE_BASE_URL=https://你的兼容接口地址/v1
COMPATIBLE_API_KEY=你的聊天模型密钥
USE_FAKE_MODEL=false

HOST=127.0.0.1
PORT=8080
LOG_LEVEL=INFO
MODE=prod
AUTH_SECRET=replace-with-your-random-token
DATABASE_TYPE=sqlite
SQLITE_DB_PATH=checkpoints.db

FEISHU_APP_ID=你的应用ID
FEISHU_APP_SECRET=你的应用密钥
FEISHU_REPLY_MODE=agent
FEISHU_AGENT_URL=http://127.0.0.1:8080
FEISHU_AGENT_TIMEOUT=75
FEISHU_QUEUE_BACKEND=redis
FEISHU_REDIS_URL=redis://127.0.0.1:6379/0
FEISHU_REDIS_DEDUP_TTL=86400

EMBEDDING_API_KEY=你的向量模型密钥
EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
EMBEDDING_MODEL=BAAI/bge-m3
QDRANT_URL=http://127.0.0.1:6333
QDRANT_COLLECTION=dev_issue_documents_bge_m3_v1
```

聊天模型与 Embedding 是两套配置。文档和问题查询须使用相匹配的向量模型与维度；Collection 不是启动时自动填充的知识库。

`DEFAULT_MODEL` 使用服务枚举 `openai-compatible`，具体厂商模型名填 `COMPATIBLE_MODEL`。后端默认 Agent 仍是框架的 `research-assistant`；飞书和评测显式选择 `dev-issue-agent`，不需要改默认 Agent。

`AUTH_SECRET` 是这两个本地应用之间的访问令牌，替换为自己的随机 ASCII 字符串，飞书和 CLI 会读取它。环境变量优先于 `.env`；修改配置后重启相应进程。改变数据库路径不会迁移旧数据，恢复旧会话须沿用原 checkpoint 和会话目录。

可选配置：

| 配置 | 作用 |
| --- | --- |
| `FEISHU_EVAL_ENABLED=true` | 开启飞书后台评测；默认开启，仅 agent 模式生效 |
| `FEISHU_EVAL_CASES_PATH=evals/supportpilot/cases.json` | 飞书完整 10 案例；快速演示可选择另存的单案例文件 |
| `FEISHU_EVAL_ALLOWED_OPEN_IDS` | 限定可发起、查询自己评测的用户，逗号分隔；留空不额外限名单 |
| `FEISHU_REPORT_FOLDER_TOKEN` | 可选报告云盘文件夹 token，应用需有写入权限 |
| `FEISHU_BYPASS_PROXY=true` | 网络确需时，仅对飞书域名绕过代理 |
| `FEISHU_FORCE_TLS12` | 默认 true，飞书网络兼容设置，仍校验证书 |
| `EMBEDDING_FORCE_TLS12=true` | 仅在向量服务 TLS 兼容问题时启用，仍校验证书 |

完整变量与默认路径见 [配置模板](../.env.example)。真实凭据只放本机 `.env`，不复制进文档或评测案例。

## 3. 飞书后台配置

配置应用机器人能力、交互范围和生效版本。事件与卡片回调使用长连接：

- 接收消息事件：`im.message.receive_v1`。
- 菜单事件：`application.bot.menu_v6`。
- 新版卡片交互回调：`card.action.trigger`，用于继续会话、反馈和查询状态。

需要接收用户单聊消息、发送及更新消息的对应应用权限。按当前飞书后台提示申请并发布生效；代码不会自动创建后台菜单或开通权限。

菜单使用“推送事件”：

| 名称 | event_key | 备用单聊命令 |
| --- | --- | --- |
| 新建会话 | `session_new` | `/new` |
| 历史会话 | `session_history` | `/history` |
| 当前会话 | `session_current` | `/current` |
| 发起回归 | `eval_start` | `/eval` |
| 查询评测 | `eval_status` | `/eval-status` |

先用文本命令也可运行，无须等菜单全部配好；卡片反馈仍需要卡片回调。

导入文档需 wiki/docx 的对应读取权限及目标文档授权。发布报告还涉及创建 docx、写入块、添加协作者、获取元数据及链接；授权响应不完整时，兼容核对才需要协作者列表接口。接口链接与实际处理策略见 [飞书评测说明](operations.md)。API 权限申请和目标文件访问授权分别核对。

## 4. 启动基础设施

```powershell
docker compose -f docker/compose.redis.yaml up -d --wait
docker compose -f docker/compose.qdrant.yaml up -d
docker compose -f docker/compose.redis.yaml exec -T redis redis-cli ping
Invoke-RestMethod -Uri http://127.0.0.1:6333/collections
```

Redis 应返回 `PONG`；Qdrant 应返回 Collection 列表，新安装时可以为空。Redis 只绑定本机 6379，Qdrant 只绑定本机 6333。

两个 Compose 有独立项目名和命名卷：`supportpilot-queue_redis_data` 与 `dev-issue-rag_qdrant_data`。普通重启保留数据；`down -v` 会删除卷，不用于日常停止。

## 5. 首次导入知识文档

已有同配置索引时可跳过。示例 URL 换成应用已获授权的真实文档链接；以下步骤会读取文档并调用外部 Embedding。

```powershell
.\.venv\Scripts\python.exe scripts/check_embedding.py
.\.venv\Scripts\python.exe scripts/read_feishu_document.py "你的已授权wiki或docx链接" --output .local/feishu-documents/demo.json --preview-chars 0
.\.venv\Scripts\python.exe scripts/chunk_feishu_document.py .local/feishu-documents/demo.json --output .local/feishu-documents/demo.chunks.json --preview-chars 0
.\.venv\Scripts\python.exe scripts/index_document_chunks.py .local/feishu-documents/demo.chunks.json --output .local/feishu-documents/demo.vectors.json
.\.venv\Scripts\python.exe scripts/query_documents.py "与你导入文档相关的问题" --preview-chars 0
```

完成标志是入库读回成功，且自然语言查询返回预期文档 ID 与出处。检索得分不代表答案正确率；没有命中时分别检查内容、Collection、模型及阈值。

当前是手动单篇导入，没有整库自动同步。同一文档版本或模型配置变化时，入库可能拒绝混写；不要通过反复改名或删除库掩盖冲突，具体规则见 [Qdrant 说明](operations.md)。

## 6. 启动后端与飞书

终端 A，启动 FastAPI 并保持运行：

```powershell
.\.venv\Scripts\python.exe src/run_service.py
```

另一个终端可以检查基础健康状态：

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8080/health
```

`/health` 的 ok 只说明应用响应，不保证模型、Embedding、Redis、Qdrant 或飞书权限全部可用。
在 `http://127.0.0.1:8080/docs` 中使用 Authorize 输入 `.env` 的 `AUTH_SECRET`，查看 `/info`，确认存在 `dev-issue-agent` 和选用模型。

终端 B，启动飞书入口并保持运行：

```powershell
.\.venv\Scripts\python.exe src/run_feishu.py
```

应看到 `飞书任务队列：redis`、`飞书回复模式：agent` 和长连接成功日志。Redis 模式启动时连接失败会退出，不会自动退回内存队列。

在机器人单聊发送问题，收到回答卡片后可填写选填意见并点击“有帮助／没帮助”。使用 `/new`、`/history` 和 `/current` 操作真实会话目录。

Redis + agent 模式会先显示接收回执，再由独立通知线程将同一张卡片更新为处理状态和最终答案。第二条问题的回执不等待第一条模型调用结束；答案仍先保存快照再展示评价按钮。恢复与通知失败处理见 [接收回执说明](operations.md)。

可选网页入口：

```powershell
.\.venv\Scripts\streamlit.exe run src/streamlit_app.py
```

页面应选择 `dev-issue-agent`。飞书业务扩展不依赖网页运行。

## 7. 评测和报告

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_supportpilot.py --validate-only
.\.venv\Scripts\python.exe scripts/evaluate_supportpilot.py --case SP01
.\.venv\Scripts\python.exe scripts/evaluate_supportpilot.py
```

第一条只校验案例；后两条要求 FastAPI 运行并会调用模型，涉及检索的案例还需要 Embedding 和已导入的 Qdrant 索引。CLI 不需要飞书长连接。

飞书发送 `/eval` 使用后台同一评测器；完成后收到摘要与报告链接。仅规则失败也会发布真实结果，`succeeded` 表示调度和发布成功，不表示所有案例通过。报告发布失败保留本地 `report.md`，不会重新跑模型来补发报告。

飞书选择案例看 `FEISHU_EVAL_CASES_PATH`；CLI 默认始终为 `cases.json`，可用 `--case SP01` 或 `--cases` 显式选择。开始的任务已保存案例快照，修改文件不会改变在途任务。

## 8. 查询与故障处理

```powershell
.\.venv\Scripts\python.exe scripts/show_message_tasks.py
.\.venv\Scripts\python.exe scripts/show_feedback.py --limit 5
.\.venv\Scripts\python.exe scripts/show_evaluation_job.py "评测任务编号"
```

这些本地命令分别查询任务、反馈和评测数据库，不发起新的模型调用；第一次运行相应功能前，任务数据库可能尚不存在。反馈查询可能显示用户输入，仅在本机排障时使用。

| 现象 | 优先核对 |
| --- | --- |
| 飞书无回应 | 入口是否仍运行、长连接、用户可用范围、事件是否生效、队列状态 |
| 启动 Redis 失败 | Docker 容器和端口、`FEISHU_REDIS_URL`、旧飞书进程是否持有 Worker 文件锁 |
| 后端连接失败或 401 | 后端地址、8080 监听、两端 `AUTH_SECRET`，以及环境变量是否覆盖 `.env` |
| 检索 `error` | `rag` 依赖、向量接口、余额与网络、Collection 配置；不同于 `no_results` |
| 检索 `no_results` | 实际入库内容、查询和阈值；不等于已证明整个知识库没有相关资料 |
| 任务 `needs_review` | 后端调用结果不明，禁止自动重跑；按下方说明核对并人工处理 |
| 任务 `failed` 且已有回答 | 发送已达到重试上限，可停止入口后使用 `retry-send`，不重复调用模型 |
| 报告 `publish_failed` | 查看任务失败阶段、API 错误码、应用权限与目标文件授权；本地产物仍在 |
| 评测 `failed` 或 `interrupted` | 区分服务前置检查、执行异常和进程中断，不将它们写成答案规则失败 |

消息中断恢复和 `abandon` / `retry-send` 命令见 [Redis 任务说明](operations.md)。先核对后台旧请求已结束并处理结果，再解除待确认任务；不要直接改数据库状态触发模型重试。

飞书报告查询和独立发布已有报告见 [评测说明](operations.md)。优先排查已有 `document_id`，避免重复创建远端文档。

## 9. 停止与保留数据

先在飞书终端按一次 `Ctrl+C`，等待当前普通任务结束，再停止 FastAPI。尚未执行的 Redis 消息任务保留；正在执行的评测按其状态机处理中断，不自动重跑。

需暂停容器时：

```powershell
docker compose -f docker/compose.redis.yaml stop
docker compose -f docker/compose.qdrant.yaml stop
```

需保留的本地文件：`checkpoints.db`（或配置路径）、`.local/feishu-sessions.sqlite3`、`.local/feishu-feedback.sqlite3`、`.local/feishu-tasks.sqlite3`、`.local/evaluation-jobs.sqlite3` 及 `evals/supportpilot/runs/`。运行时可能存在 SQLite 日志文件；本版没有自动备份功能，做文件备份时先正常停止应用，并保留对应 Docker 卷。

根目录 `compose.yaml` 和 `docker/Dockerfile.service` 属于原框架部署路径，尚未覆盖新增顶层业务模块，不作为本版完整容器化启动方式。
