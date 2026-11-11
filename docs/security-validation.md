# 公网演示安全验证记录

验证日期：2026-11-11。范围为仓库代码、隔离容器中的公开演示配置、真实 LiteLLM 与本地模拟模型上游。没有调用付费模型，没有对线上第三方系统做渗透测试。

## 已验证

- 完整 Linux pytest：584 通过、4 跳过。跳过包括需要单独栈的网关测试和既有可选集成；旧 Docker 测试文件在本轮全量运行中排除，另使用新的真实网关/HTTPS 栈验收。
- 真实 LiteLLM 1.108.4：普通聊天、SSE、Tool Calling、Embedding、模型白名单、普通 Virtual Key 无法管理密钥、无密钥拒绝、零预算拒绝通过。该版本预算超限可能返回 HTTP 422。
- 完整 Docker 公网栈：PostgreSQL 初始化独立业务/网关库与非超级用户；NGINX TLS、反代、登录、Secure/HttpOnly Cookie、普通成员不能创建团队或上传、4000 字符限制、Origin 检查、后端聊天和 SSE 经 LiteLLM 返回模拟结果。
- 后端只接收必要的业务配置，不接收 PostgreSQL 超级用户或 LiteLLM 数据库密码；NGINX 动态解析内部服务，支持容器更新后的地址变化。
- 账号回归：注册字段拒绝角色注入、未验证邮箱不能登录、验证码哈希保存/单次消费、密码重置撤销全部会话、账号停用撤销会话、普通成员不能访问账号管理接口。
- 费用回归：同用户跨线程并发互斥、每日次数、全站每日次数独立于账号、网关密钥加密保存及重用。限流故障按拒绝处理。
- 静态检查：Ruff、格式检查、Pyrefly 和 GitHub Actions actionlint。
- 前端功能测试与生产构建通过；新增注册页面测试覆盖人机验证前的禁用状态和验证码提交。
- 新增安全用例单独运行：9 通过；前端最终 21 通过。完整 HTTPS 栈验证了 NGINX 登录限速返回 429。
- 本机模拟调用后空闲内存：后端约 144 MiB、LiteLLM 约 647 MiB、PostgreSQL 约 66 MiB、Qdrant 约 80 MiB，其余约 33 MiB，合计约 970 MiB。该数据不包含真实文档规模、备份和负载峰值，部署仍建议 4 GB 起。

## 依赖审计范围

公网镜像 `docker/Dockerfile.public` 安装独立 `public` 依赖组，扫描实际 `/opt/venv` 的 **81 个包，pip-audit 未发现已知漏洞**。这不覆盖基础操作系统、NGINX、Qdrant 或 LiteLLM 自身镜像，也不表示未知漏洞不存在。

原通用镜像继续服务本地示例，包含 Chroma、Streamlit、PDF、Mongo 等依赖。不要将通用镜像作为公网后端。公网模式只加载 dev-issue-agent，不导入这些示例依赖；保留本地示例功能和既有回归。

升级了 Postgres checkpoint、LangGraph SDK、PDF、GitPython、Brotli、DNS、OAuth、ASN.1、setuptools 与 OpenTelemetry 相关补丁。部分包仍在通用开发环境中存在历史公告；公网运行时通过精简依赖避免携带这些包。安全补丁单独豁免 14 天发布冷却，例外位于 `pyproject.toml`，不是全局关闭冷却。

审计原始 JSON 存放在被忽略的 `.tmp/security-audit/`；不提交原始用户数据、凭据、令牌或数据库快照。

## CI 门禁

1. lint、类型、pytest（真实 PostgreSQL/Redis）、前端测试/构建。
2. `gateway` job：真实 LiteLLM + 模拟上游，以及使用临时测试证书的完整 HTTPS 公网栈。
3. 公网镜像实际运行时 pip-audit；存在已知漏洞时阻止该镜像发布。
4. 验证后向 GHCR 发布通用后端、公网后端和前端镜像。公网部署使用 `backend-public`。

测试凭据只适用于临时隔离栈，测试证书为一天有效期的自签名证书。生产部署必须使用真实 CA 证书，测试脚本中关闭 TLS 验证的写法不用于业务请求。

## 服务器上仍需验收

- 配置自己的 Resend、发信域名及 Turnstile，验证真实收信、过期处理和重置流程。自动测试模拟外部验证和邮件接口。
- 配置真实模型和向量模型，验证 Tool Calling、usage、计费单价、向量维度及召回结果；自动测试使用合成上游。
- 使用真实域名证书检查 HTTPS、续期、Cookie 和安全组。
- 演练 PostgreSQL、LiteLLM、Qdrant 恢复；加密备份保存到服务器外。
- 预算使用准确价格和上游 usage 计算；在途请求会产生费用。业务层次数、并发、输出上限和厂商限额共同限制风险。
- 账号停用阻止新会话及新请求；已经发送的模型请求可能完成。

以上是当前版本的回归与部署检查记录，不是第三方安全认证。
