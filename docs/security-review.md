# 公网上线与注册功能检查（2026-11-11）

> 下文保留首次检查时的风险记录。随后已实施公网网关、开放邮箱验证注册、权限收紧、额度控制及精简公网运行时。当前实现和验证范围见 [安全验证记录](security-validation.md)，上线步骤见 [阿里云部署](aliyun-deployment.md)。首次检查的复现结果不代表当前版本仍保留相同行为。

## 结论与范围

当前版本有登录和租户隔离基础，但不建议直接开放给不受信任的公网用户，也不建议立即增加任意邮箱自由注册。先完成下述上线阻断项，再以邀请码方式开放小范围体验。

本次进行了源代码审查、本地 ASGI 模拟验证、Qdrant 本地实现验证、运行镜像 Python 依赖审计、前端 npm 锁文件审计。没有攻击线上站点，没有调用真实模型或产生模型 API 费用。模拟验证不能代替真实平台部署验收或完整渗透测试。

## 已有的防护

- 会话 Token 使用随机 32 字节生成，仅保存 SHA256 摘要；Cookie 为 HttpOnly、SameSite=Strict，有效期 12 小时，支持退出撤销当前会话。
- 密码有随机盐并使用 scrypt 哈希；参数仍需升级，详见后续项。
- 会话目录先验证团队、用户及 Agent 归属，再映射到服务端生成的 checkpoint ID。
- PostgreSQL 业务访问带有租户/用户作用域，向量查询同时过滤租户与已登记的文档 ID。
- 团队模式关闭旧共享密钥入口、其他示例 Agent 和旧反馈接口；模型不能通过工具参数选择租户。
- 导入向量文件限制 10 MB；React Markdown 没有开启原始 HTML 渲染，外部图片替换为文字，链接限制 HTTP(S)。
- 当前团队部署没有发布 PostgreSQL 和 Qdrant 端口；默认只将网页绑定到本机。

以上是代码中可见的防护，不构成“已经不存在越权或 XSS”的保证。

## 上线前必须完成

### 1. 登录没有速率限制

证据：[登录接口](../src/tenancy/api.py#L157)、[密码验证](../src/tenancy/store.py#L31)、[Nginx 配置](../docker/nginx.conf)。

本地模拟中，同一客户端连续 25 次错误密码请求全部返回 401，没有进入 429 或退避。每次请求都会执行 scrypt，除了密码猜测，还能持续占用 CPU 和哈希工作线程。

需要按账号和可信客户端 IP 联合限速、失败退避与审计；限制同时执行的密码哈希数量。经过 Nginx/平台代理时，必须正确配置可信代理，不能直接信任客户端提交的 X-Forwarded-For。多实例的计数应使用共享存储。

### 2. 模型调用与持久化资源没有账号级预算

证据：[请求模型](../src/schema/schema.py#L50)、[模型选择与运行准备](../src/service/service.py#L193)、[Agent 消息上下文](../src/agents/dev_issue_agent.py#L92)、[运行锁](../src/tenancy/store.py#L211)。

- 前端 textarea 限制 20,000 字符，后端 `UserInput.message` 没有 `max_length`；本地模拟确认 25,000 字符问题可被接受。
- 数据库仅限制“同一会话一个 running”。模拟中 6 个不同会话可同时进入模型执行路径，没有账号级或全局并发门槛。
- 没有每日提问额度、Token/费用预算、团队/成员/文档总量限制。
- 当前 Agent 将整个历史交给模型，没有固定的历史 Token 窗口。
- 用户可以在服务已配置的模型中选择，没有演示账号专属的模型白名单。

需要在服务端、进入模型与资源创建之前，原子检查/占用请求额度和并发名额；限制消息长度、累计上下文、输出 Token 和可选模型。客户端按钮状态不能作为额度控制。出错、中断和结果未知时的计数需要定义清楚，不能靠刷新网页重置。

### 3. 普通成员可以绕开“仅管理员创建账号”的演示边界

证据：[创建团队接口](../src/tenancy/api.py#L201)、[创建团队即成为管理员](../src/tenancy/store.py#L141)、[成员创建](../src/tenancy/store.py#L160)。

任何登录成员都能创建新团队并成为该团队管理员；管理员又能创建新用户、添加成员和导入文档。模拟确认普通成员可得到新团队的 admin 角色。这没有越权成为原团队的管理员，但意味着一个低权限体验账号可以继续创建账号、团队和上传资源。

需要区分站点准入权限与团队角色。演示账号即便拥有新工作空间，也不应因此获得无限创建团队、邀请用户、上传资料的能力。额度不能仅绑定到可无限创建的团队。

### 4. 首次租户导入可以影响共享向量索引配置

证据：[导入直接传递文件](../src/tenancy/api.py#L233)、[以文件元数据创建 Collection](../src/qdrant_store.py#L51)。

前提是共享 Collection 尚未创建。此时维度、embedding_model 和 embedding_base_url 都取自上传文件，没有先与服务端认可的 Embedding 配置核对。

使用真实 Qdrant 本地实现验证：租户 A 首先导入一份带自定义模型信息的合法向量文件，随后租户 B 使用正常模型文件导入同一 Collection，被全局配置不匹配拒绝。现有 Collection 会拒绝不匹配文件；本次没有发现可用此路径覆盖已有 Collection 配置或读取其他租户正文的证据。

需要由服务端预置/固定模型、维度和距离配置，在接触 Qdrant 之前验证文件；租户管理员不能决定共享资源的全局配置。备选方案是按明确的服务端模型配置划分索引，仍然校验租户作用域。此项是前一版实现中遗漏的信任边界。

### 5. 当前运行依赖需要安全更新和裁剪

对 `supportpilot-backend:ci-check` 运行镜像的 212 个 Python 包进行了 pip-audit 检查。镜像 `/app/uv.lock` 与仓库 `uv.lock` 的 SHA256 一致。

结果为：14 个包命中公告；工具原始输出 97 条匹配，按“包名＋公告 ID”去重为 95 条。不同 ID 之间还可能属于同一漏洞的别名，不能将这个数字理解为 95 个已确认的公网可利用漏洞。需要按实际入口、调用方式和公告逐项确认。

值得优先处理的示例：

| 包 | 镜像版本 | 扫描指出的修复版本 / 条件 |
| --- | --- | --- |
| langgraph-checkpoint-postgres | 3.1.2 | 3.1.3；涉及 Store namespace 标签中的 `.` 导致作用域碰撞；当前业务 Agent 未直接暴露任意 Store namespace 操作，未确认该应用可被直接利用 |
| langgraph-sdk | 0.4.4 | 0.4.6；涉及 v3 远程流式客户端路径中的不可信标识；当前本地图执行未确认触发该路径 |
| pypdf | 6.19.0 | 多条公告要求 6.20.x / 6.21.x；当前约束 `<6.20.0` 会阻止这些修复版本；团队 API 目前不接收原始 PDF |
| chromadb | 1.5.9 | 包括 Chroma HTTP 服务相关公告；当前部署没有运行 Chroma 服务，不能据此宣称当前 API 暴露了相同漏洞 |

其他命中包：brotli、dnspython、filelock、gitpython、oauthlib、opentelemetry-api、pillow、pyasn1、pymongo、setuptools。

前端 `npm audit` 当前报告 0 条已知漏洞。该结果仅代表本次漏洞库检查，不替代业务安全审查。

需要更新实际使用的依赖；将 Chroma、MongoDB、PDF、旧示例 Agent 等非团队部署必需能力移出最小运行依赖，并调整导入边界。不能只卸载包而让服务启动时仍无条件导入。安全补丁必要时应对 14 天依赖冷却策略做受控例外，更新锁文件、重建镜像、回归后复扫。当前 CI 尚未包含依赖漏洞门禁。

### 6. 生产部署配置需要明确的启动校验

证据：[本地脚本生成非 Secure Cookie](../scripts/start.py#L36)、[HTTP Nginx](../docker/nginx.conf#L2)、[旧模式可不配置共享认证](../src/service/service.py#L88)。

本地启动脚本默认生成 `TENANT_COOKIE_SECURE=false`，这是本地 HTTP 的配置，不能原样作为公网生产配置。当前 Compose 强制开启团队模式，但单独把后端镜像部署到平台时，仍需显式开启 `TENANT_MODE=true`。错误退回旧模式且未配置 AUTH_SECRET 时，接口只告警而不会拒绝启动。

公网应使用平台 HTTPS/可信反向代理、Secure Cookie 和明确的站点 Origin。生产模式应验证这些配置并拒绝不安全的回退；数据库与向量库继续保持私网。备份和恢复也应实际验证。

## 应继续补齐的账号安全能力

- 账号停用、移除成员、撤销用户全部会话；目前只有退出当前会话，没有完整的止损入口。
- 用户自行设置/修改密码与安全找回；管理员当前可以设置新账号初始密码，但没有首次登录改密流程。
- 当前 scrypt 使用 `N=2^14,r=8,p=1`，低于 OWASP 当前推荐的对应组合。应选合适的 Argon2id 或更强 scrypt 参数，支持参数版本与旧密码验证成功后的重哈希，并与哈希并发上限一起设计，避免提高参数后放大登录内存消耗。[OWASP 密码存储建议](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)
- CSRF 已检查 Origin 和 Fetch Metadata，但两类请求头都缺失时仍允许 Cookie 写请求。后续应区分 Cookie 与显式 Bearer 认证，对 Cookie 写操作提供明确、可测试的来源/CSRF Token 策略。本次未证明已能通过普通跨站页面利用这一缺口。[OWASP CSRF 建议](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)
- 审计邀请、角色变更、密码重置、账号停用、模型额度消耗等关键事件，避免记录明文密码、会话 Token 和模型密钥。

## 注册功能建议

第一阶段采用邀请码注册：管理员邀请 → 用户打开注册页并自行设置密码 → 服务端消费单次、限时、可撤销的邀请 → 自动加入邀请指定团队和角色 → 使用受限额度。

邀请中的团队、角色和额度由服务端记录决定，不能相信注册请求提交的 tenant_id / role。邀请 Token 仅保存摘要，并以事务保证单次消费。通过指定邮箱发送的验证链接可以验证邮箱控制权；手动发放的体验码本身不能被标记为已验证邮箱。

先修复准入权限与额度边界，再提供少量体验邀请。自由注册应在邮件验证、重置/封禁、邀请或注册限速、反自动化与账号配额完备后考虑。对求职演示，没有必要默认给所有访客创建团队和上传知识库的能力。

## 本地验证记录

以下是模拟业务存储与模型的 ASGI 验证，以及 Qdrant 本地实现验证；没有访问线上服务：

```json
{
  "login_failures_without_429": 25,
  "server_accepts_25000_character_question": true,
  "member_can_create_team_and_become_its_admin": true,
  "simultaneous_distinct_thread_invocations_accepted": 6,
  "first_tenant_import_can_block_other_tenant_embedding_profile": true,
  "real_model_requests": 0
}
```

本地原始审计结果保存在 `.tmp/security-audit/runtime.json` 和 `.tmp/security-audit/npm.json`，不提交版本库。本报告记录检查时的状态，尚未对上述问题实施运行时代码修复。
