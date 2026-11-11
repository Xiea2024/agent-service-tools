# SupportPilot 多租户最小闭环

这一版本面向网页团队工作台：登录 → 选择团队 → 导入团队知识库 → 提问与继续本人会话 → 保存回答与提交反馈。业务状态和 LangGraph checkpoint 使用 PostgreSQL，向量继续使用 Qdrant。

这是可本地演示和验证的第一版。默认 `TENANT_MODE=false`，原飞书与匿名网页方式继续工作。启用后，仅开放已经适配权限的 SupportPilot API；飞书使用的共享服务密钥、AG-UI、示例 Agent 和博客入口均不能访问多租户服务。可另运行一个关闭多租户的服务保留原入口，不要共用多租户业务数据或索引权限。

## 1. 本地启动

保留已有 `.env`，按需修改下列配置。不要把自己的密码写进版本库。

```dotenv
TENANT_MODE=true
DATABASE_TYPE=postgres
POSTGRES_USER=supportpilot
POSTGRES_PASSWORD=请设置自己的数据库密码
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_DB=supportpilot
# 仅本地 HTTP 演示设为 false；HTTPS 部署保持 true。
TENANT_COOKIE_SECURE=false
```

沿用已有模型、Embedding 和 Qdrant 配置。数据库依赖已经在项目中，无需增加 ORM。

```powershell
docker compose --env-file .env -f docker/compose.postgres.yaml up -d --wait
docker compose -f docker/compose.qdrant.yaml up -d
.\.venv\Scripts\python.exe scripts/create_tenant_admin.py --email admin@example.com --team "研发团队 A"
.\.venv\Scripts\python.exe src/run_service.py
```

管理员脚本交互输入密码，不把密码放进命令历史。脚本只创建新账号，不重置已有用户。它会先运行业务表迁移；服务启动也会检查迁移版本。请先备份目标数据库，不要使用已有生产数据库进行演示。

另一个终端运行前端：

```powershell
cd frontend
npm run dev
```

打开 `http://127.0.0.1:5173/supportpilot` 登录。Vite 在多租户模式下不再注入共享 `AUTH_SECRET`，浏览器使用 HttpOnly 会话 Cookie。会话有效期 12 小时，退出时服务端立即撤销。服务和前端改变配置后都需要重启。

首次用户可在“团队与知识库”中创建另一个团队、添加新账号或把已有账号加入当前团队。添加已有账号不会重置其密码。第一版不提供公开注册、密码找回、成员删除或角色修改。

## 2. 导入团队文档

复用现有飞书读取与切分脚本，拿到 `*.chunks.json` 后，只生成向量文件：

```powershell
.\.venv\Scripts\python.exe scripts/index_document_chunks.py .local/example.chunks.json --prepare-only
```

`--prepare-only` 会请求 Embedding 并保存 `*.vectors.json`，不连接或写入 Qdrant。团队管理员登录后，在“团队与知识库”选择该向量文件上传，最大 10 MB。

导入 API 从登录上下文确定租户，不接受文件指定权限。写入、读回验证完成后才登记 PostgreSQL 文档目录；同一团队/文档的导入通过数据库锁串行化。相同文件可重试，不同版本仍明确拒绝，尚不支持更新删除同步。

PostgreSQL 与 Qdrant 之间没有分布式事务。中断可能留下部分向量，但检索同时过滤租户和 PostgreSQL 中已完成登记的文档编号，未登记片段不会进入回答。使用相同文件重试可补齐。不同团队可导入同一篇原始文档，Point ID 包含租户作用域，互不覆盖。

历史无租户向量不会自动归属某个团队；需要管理员重新导入。旧匿名会话和飞书 SQLite 数据也不会自动转换成有身份归属的数据。

## 3. 权限与数据边界

| 资源 | 访问规则 |
| --- | --- |
| 团队知识库、成员列表 | 当前团队成员可读 |
| 新增成员、导入文档 | 当前团队管理员可操作 |
| 创建新团队 | 已登录用户可创建，创建者成为管理员 |
| 会话、运行记录、回答、反馈 | 同时匹配当前团队和登录用户；管理员也不能读取他人的会话 |
| 反馈 | 仅回答所属用户可提交；首次有效评价生效 |

浏览器的 `X-Tenant-ID` 只是团队选择，服务端每次验证成员关系。请求中的 `user_id` 不作为权限依据；内部用户上下文包含租户作用域。`agent_config` 在团队模式下拒绝，避免覆盖身份或 checkpoint 配置。

客户端会话编号先经过业务目录归属检查，再映射到后端生成的随机 checkpoint 编号，不能通过提交旧 checkpoint 编号读取历史。身份上下文贯穿 SSE 响应和工具调用；模型工具参数只有检索问题，不包含租户选择。

业务表前缀为 `sp_`，迁移位于 `src/tenancy/migrations/`。业务表与 LangGraph 自己的表分开维护。迁移通过事务和 advisory lock 防止多个服务同时建表；约束和索引负责成员关系、引用完整性、首次反馈及同一会话单运行限制。当前隔离使用应用层授权和复合外键，没有声称实现数据库 RLS。

## 4. 运行状态与恢复边界

会话同一时间只允许一个模型运行；冲突在发送 SSE 响应头之前返回 HTTP 409。不同会话可独立执行。

`sp_runs` 记录 `running / succeeded / failed / interrupted`。成功运行将最终回答与工具证据保存为不可变快照；模型异常记为失败，正常处理到的取消记为中断。`GET /runs/{run_id}` 可以查看自己的记录，`POST /runs/{run_id}/feedback` 提交评价。

进程强杀可能留下 `running`，因为客户端断线不能证明模型调用已停止。这一版不会自动重跑或按超时释放执行锁。运维人员必须先确认旧服务及相关调用已经停止，再将相应 `sp_runs` 记录标记为 `interrupted`，之后重新加载历史核对 checkpoint，再决定是否继续。PostgreSQL 业务快照与 checkpoint 的提交也不是同一个事务，不承诺严格一次执行。

## 5. 验证

检索隔离测试使用真实的 Qdrant 本地实现与合成向量，不调用模型：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_tenant_retrieval.py -q
```

数据库集成测试必须显式提供独立测试数据库连接。每个测试创建随机 schema，并在结束时删除该测试 schema，不读取应用的 PostgreSQL 配置。

```powershell
$env:TEST_POSTGRES_DSN='postgresql://测试用户:测试密码@127.0.0.1:55432/测试数据库'
.\.venv\Scripts\python.exe -m pytest tests/test_tenancy_postgres.py -q
```

覆盖迁移重入、事务约束、登录与撤销、团队选择、成员权限、越权读取和反馈、SSE 授权、PostgreSQL checkpoint 恢复、文档导入与隔离。模型使用合成回复，不代表真实模型效果或生产压测结果。

前端验证：`npm test`、`npm run build`。发布到公网之前仍需完成部署层的 HTTPS、登录限流、备份与恢复配置，以及真实模型和浏览器的部署验收；这一版本没有加入 SSO、配额计费或生产高可用能力。
