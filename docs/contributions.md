# SupportPilot 业务扩展与贡献说明

本文用于说明项目来源、业务扩展范围及可定位的实现证据。它不是生产履历或商业指标证明。项目采用 AI 辅助开发，贡献以需求取舍、集成实现、验证和问题处理为依据，不以手写代码比例描述。

## 1. 原框架与业务扩展的边界

| 层次 | 原框架提供 | SupportPilot 扩展与适配 |
| --- | --- | --- |
| Agent 与服务 | LangGraph 注册、FastAPI 路由、RunnableConfig、模型工厂 | 研发问题 Agent、检索工具循环、业务 Prompt、轮次与耗时预算 |
| 客户端 | AgentClient 同步/异步调用和流式协议 | 飞书桥接和评测复用客户端，显式选择业务 Agent、线程和认证 |
| 会话存储 | SQLite / PostgreSQL / MongoDB 等 checkpointer 集成 | 飞书会话目录、当前线程、菜单切换、归属校验与事件幂等；不重写 checkpoint 引擎 |
| 界面与渠道 | Streamlit、通用消息协议和示例 Agent | 飞书长连接、会话与回答卡片、反馈回调、后台评测入口 |
| 知识检索 | 仓库原有 Chroma / Bedrock 等独立示例 | 单篇飞书文档读取、切分、独立 Embedding、Qdrant 导入与业务检索 |
| 反馈与评测 | 框架已有其他反馈集成及测试基础 | 本地不可变回答快照、首个评价、固定案例、共享评测器、原生飞书报告与通知 |
| 任务可靠性 | 原框架服务运行能力 | SQLite 消息阶段台账、Redis Streams、去重 TTL、发送重试和中断待确认 |

“仓库中包含某技术”与“SupportPilot 已使用并验证该技术”分别描述。例如仓库有多 Agent Supervisor 示例，但本业务尚未接入；Store 接口存在，但 SQLite 模式下跨会话 Store 并未持久化。

## 2. 核心扩展与证据

| 贡献范围 | 主要代码 | 可查看的验证证据 |
| --- | --- | --- |
| 研发问题问答 | [dev_issue_agent.py](../src/agents/dev_issue_agent.py)、[agents.py](../src/agents/agents.py) | [Agent 测试](../tests/agents/test_dev_issue_agent.py)、[RAG 图测试](../tests/agents/test_dev_issue_rag.py) |
| 飞书渠道与网络适配 | [run_feishu.py](../src/run_feishu.py) | [桥接测试](../tests/test_feishu.py)、[SDK 会话测试](../tests/test_feishu_session_sdk.py) |
| 文档入库与检索 | [feishu_documents.py](../src/feishu_documents.py)、[document_chunks.py](../src/document_chunks.py)、[embedding_client.py](../src/embedding_client.py)、[qdrant_store.py](../src/qdrant_store.py)、[document_retrieval.py](../src/document_retrieval.py) | [切分](../tests/test_document_chunks.py)、[向量库](../tests/test_qdrant_store.py)、[检索](../tests/test_document_retrieval.py) 与对应 CLI |
| 会话管理 | [feishu_sessions.py](../src/feishu_sessions.py)、[feishu_cards.py](../src/feishu_cards.py) | [会话 SQL 测试](../tests/test_feishu_sessions.py)、[菜单测试](../tests/test_feishu_session_ui.py) |
| 回答反馈 | [feedback.py](../src/feedback.py)、[feishu_feedback.py](../src/feishu_feedback.py) | [持久反馈测试](../tests/test_feedback.py)、[真实 SDK 离线接线](../tests/test_feishu_feedback_sdk.py) |
| CLI 评测与结果留存 | [evaluation.py](../src/evaluation.py)、[evaluate_supportpilot.py](../scripts/evaluate_supportpilot.py)、[案例集](../evals/supportpilot/cases.json) | [评测器测试](../tests/test_evaluation.py)、本机运行目录中的 JSONL 和 report |
| 飞书后台评测与报告 | [evaluation_jobs.py](../src/evaluation_jobs.py)、[feishu_evaluation.py](../src/feishu_evaluation.py)、[feishu_reports.py](../src/feishu_reports.py) | [任务状态测试](../tests/test_evaluation_jobs.py)、[发布测试](../tests/test_feishu_reports.py)、[SDK 闭环测试](../tests/test_feishu_evaluation_sdk.py) |
| Redis 任务恢复 | [feishu_tasks.py](../src/feishu_tasks.py)、[Redis Compose](../docker/compose.redis.yaml)、[查询脚本](../scripts/show_message_tasks.py) | [Streams 与进程强杀测试](../tests/test_feishu_tasks.py)、[恢复与持久化记录](operations.md) |

原有服务、模型适配、客户端和 SQLite 初始化也有集成调整；不把这些基础模块整体归为从零编写。上表是业务范围索引，不是每一行代码作者的证明。

## 3. 关键设计取舍

### 同一问答入口服务用户和评测

飞书用 `AgentClient.invoke`，评测用 `AgentClient.astream`，都请求同一个 `dev-issue-agent`。评测需要完整工具轨迹，因此选择完整消息流，而非在脚本中另写 Prompt 和检索逻辑。

### 会话目录与模型历史分开

SQLite 业务目录管理“用户当前选中了哪个 thread”，LangGraph checkpoint 管理该 thread 的历史消息。新建会话真实生成线程并保存选择，菜单动作不依赖模型口头宣称成功。恢复时需要同时保留目录和 checkpoint。

### 先保存回答快照，再发可评价卡片

反馈提交关联不可变的问题、答案、线程、运行和用户，而不是只存一条“没帮助”。卡片只传快照标识，操作者从平台事件取得并检查归属；事务和唯一约束保证首个有效评价生效。

### Redis 负责任务交接，SQLite 保存业务进度

Streams 提供入队、领取、未确认记录及 ACK，SQLite 保存顺序、执行阶段和已有答案引用，并填补数据库提交后 Redis 尚未写入的间隙。默认一个 Worker 保持问题与会话切换顺序；没有为了并发而直接增加消费者。

### 结果不明时优先避免重复模型调用

客户端超时不证明后端已经停止。已有快照可继续发送，无快照的在途调用进入待确认状态，保留线程关联供排查。明确承认需要人工处理的窗口，而非宣称队列能自动保证业务严格一次执行。

### 本地评测结果与远端报告发布分开

先落盘逐轮结果，再发布飞书报告；任务记录保留 document_id、失败阶段及通知状态。发布失败不丢本地报告、不自动重新评测，通知失败也不覆盖已发布状态。规则结果与答案质量分别呈现。

## 4. 验证证据应如何表述

- 自动测试：2026-11-04 的相关回归记录为 182 项通过，覆盖当时选定的飞书、会话、反馈、评测、报告和 Redis 测试；不是整个仓库或所有环境的通过证明。
- 真实基础设施：已验证本机 Redis Streams 和容器重建后记录保留；强制结束专用测试进程验证三个恢复位置，模型和飞书 API 使用模拟接口。
- 真实业务：已有飞书 RAG 使用反馈、固定案例实际运行与报告 API 联调记录；最新整条链路及真实阅读权限仍需按交付验收复核，不直接把历史或模拟网络结果当成最新实测。
- 模型质量：固定案例的规则分数不等于正确率。人工复核尚待完成，链接规则的已知误判尚未修复，不据此编造准确率提升。
- 规模：目前仅完成本机部署验证，没有真实生产流量、SLA、成本下降或高并发提升数据。

评测运行目录和本地数据库可能含私有资料，默认不提交。对外展示可另准备脱敏报告，注明日期、案例版本、模型、样本数和已知限制。

## 5. 可用于项目介绍的表述

> 自主研发问题处理助手 SupportPilot，接入飞书单聊、文档检索和多轮会话。围绕业务使用补充回答快照与反馈、复用同一 Agent 的固定案例评测、飞书报告发布，以及 Redis Streams 配合 SQLite 的任务阶段恢复。项目保留工具证据和运行标识，针对重复事件、发送失败和调用结果不明分别处理。
