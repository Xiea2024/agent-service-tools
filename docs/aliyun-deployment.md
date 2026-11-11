# 阿里云公开演示部署

本方案运行 NGINX、React、FastAPI、LiteLLM、PostgreSQL、Redis 和 Qdrant。公网仅开放 NGINX 的 80/443；其余服务没有宿主机端口。使用 `docker/compose.public.yaml`，不要把本地 `compose.app.yaml` 直接发布到公网。

## 准备服务器与账号

- 推荐 Ubuntu 24.04、2 核 4 GB、至少 40 GB 磁盘。4 GB 是包含 LiteLLM 的保守起点，实际占用见安全验证报告；模型使用外部 API，无需 GPU。
- 配置域名 A 记录到服务器公网 IP。中国内地服务器使用域名提供网站服务前完成 ICP 备案；香港等地域的具体要求与服务可用性以阿里云说明为准。
- 安全组开放 TCP 80/443；SSH 22 仅允许自己的 IP。不要开放 4000、5432、6333、6379、8080。
- 按 [Docker Ubuntu 官方文档](https://docs.docker.com/engine/install/ubuntu/)安装 Docker Engine 和 Compose 插件，并安装 Git、Python 3。
- 准备 Resend 发信域名及 API Key、Cloudflare Turnstile 站点及 Secret Key。Turnstile 绑定实际域名；生产环境不能使用测试密钥。
- 准备聊天模型与 Embedding 的厂商 API Key。建议厂商侧另设消费限额或小额余额。

```bash
git clone https://github.com/Xiea2024/agent-service-tools.git
cd agent-service-tools
python3 scripts/public_deploy.py init --domain demo.example.com
```

`init` 生成 `.env.public`、`.env.gateway` 和证书目录；已有配置时拒绝覆盖。文件默认权限 600，Git 和 Docker 构建上下文均排除密钥、证书和备份。

## 配置模型网关

编辑 `.env.gateway`，填入厂商参数。例如 OpenAI 兼容厂商需要 `openai/真实模型名称`，`UPSTREAM_*_BASE` 是厂商兼容 API 的 `/v1` 地址。

```dotenv
UPSTREAM_CHAT_MODEL=openai/你的聊天模型名称
UPSTREAM_CHAT_BASE=https://厂商域名/v1
UPSTREAM_CHAT_KEY=你的聊天密钥
UPSTREAM_EMBEDDING_MODEL=openai/你的向量模型名称
UPSTREAM_EMBEDDING_BASE=https://厂商域名/v1
UPSTREAM_EMBEDDING_KEY=你的向量密钥
```

真实厂商密钥只进入 LiteLLM。后端请求使用服务端生成并加密保存的个人 Virtual Key；不要把 `GATEWAY_MASTER_KEY`、加密密钥或 Virtual Key 填入前端。

`docker/litellm.yaml` 的模型价格是保守的演示示例，单位为 **美元/Token**。上线前按实际厂商价格配置 `model_info.input_cost_per_token` / `output_cost_per_token`，并检查真实 usage 和计费记录。价格配置错误会影响预算准确性。全站预算为 5 美元/30 天，个人预算为 0.1 美元/24 小时；预算是后续请求的拒绝条件，在途请求可能继续产生费用，不是厂商账单的绝对硬上限。

业务层默认每人每日 10 次、全站每日 60 次、每人并发 1、全站并发 3、单次输入 4000 字符、输出 2000 Token。失败任务也计入次数；每日次数按 UTC 日期重置。邮箱默认全站 80 封/滚动 24 小时、激活 30 个/滚动 24 小时。可在 `.env.public` 设置同名 `DEMO_*` 配置，详见 `src/tenancy/security.py`。

需要立即暂停新付费任务时，设置 `.env.public` 的 `PAID_CALLS_ENABLED=false` 并重建后端容器；已发往厂商的请求可能完成。不要关闭认证来排查网关问题。

## HTTPS 证书

通过阿里云数字证书管理或其他可信 CA，为实际域名签发证书。下载 NGINX 格式，放置为：

```text
docker/tls/fullchain.pem
docker/tls/privkey.pem
```

设置私钥权限 600。证书必须包含完整链，不能在公开环境使用自签名证书。证书到期前续签，替换后运行：

```bash
docker compose --env-file .env.public -f docker/compose.public.yaml exec nginx nginx -t
docker compose --env-file .env.public -f docker/compose.public.yaml exec nginx nginx -s reload
```

本仓库不自动购买域名、申请证书或办理备案。不要在证书和域名配置完成前开放注册。

## 拉取并启动

等待 GitHub Actions 成功发布三个业务镜像：`agent-service-tools-backend-public`、`agent-service-tools-web`、原有 `agent-service-tools-backend`。公网栈只使用前两个。

`.env.public` 中 `IMAGE_TAG` 推荐填写本次工作流的 `sha-完整提交SHA`，便于回滚。GHCR Package 需允许服务器拉取；私有包使用只读 `read:packages` 凭据，通过 `docker login ghcr.io` 登录，不要把 Token 写入仓库。

```bash
python3 scripts/public_deploy.py check
python3 scripts/public_deploy.py pull
python3 scripts/public_deploy.py up
```

也可以在服务器构建：先运行 `python3 scripts/public_deploy.py build`，再运行 `up`。构建会临时使用较多内存和磁盘，优先使用 CI 镜像。

PostgreSQL 初始化为业务库 `supportpilot` 与网关库 `litellm`，分别由非超级用户 `sp_app`、`sp_gateway` 持有。初始化脚本仅在空数据卷执行。不要把旧栈的数据卷直接套用到新栈，也不要在升级时删除卷。

## 初始化管理员与演示知识库

默认注册关闭。创建管理员及演示团队：

```bash
docker compose --env-file .env.public -f docker/compose.public.yaml exec backend \
  python scripts/create_tenant_admin.py --email 你的邮箱 --team 演示团队
```

密码交互输入，不进入命令历史。该部署命令拥有创建平台管理员的权限，仅允许服务器维护者执行。保存输出的团队 UUID，填入 `.env.public` 的 `DEMO_TENANT_ID`。

使用管理员登录 `https://你的域名/supportpilot`，导入专门用于展示的脱敏向量文件。上传文件的模型名必须是 `demo-embedding`、Base URL 必须是 `http://litellm:4000/v1`、维度必须与 `.env.public` 中 `DEMO_EMBEDDING_DIMENSION` 一致（默认 1024）。网关别名对应的底层模型必须与生成这些向量时使用的模型完全相同；只改文件标签不能改变向量空间。已有其他模型的 Collection 请新建迁移，不能混写。

公开用户共享该演示团队的知识库，但只可读取自己的会话、运行记录与反馈。不要导入内部文档或客户数据。

## 开放邮箱注册

编辑 `.env.public`：

```dotenv
DEMO_TENANT_ID=上一步的团队UUID
RESEND_API_KEY=你的Resend密钥
MAIL_FROM=SupportPilot <noreply@已验证发信域名>
TURNSTILE_SITE_KEY=你的站点Key
TURNSTILE_SECRET_KEY=你的SecretKey
REGISTRATION_ENABLED=true
```

```bash
python3 scripts/public_deploy.py up
```

注册需通过人机验证，邮件中收到的验证码需在页面粘贴，15 分钟有效、一次使用。验证码只存哈希，重置密码撤销全部旧会话。账号只能获得普通成员权限；无法通过注册字段、创建团队或修改前端获得管理员权限。

注册配置缺失时入口关闭。首次正式开放前，用自己的两个邮箱验证注册、收信、登录、找回密码、退出全部会话及会话隔离。邮件投递、真实模型参数和 TLS 需要使用你自己的服务凭据在服务器上验收。

平台管理员可通过受认证且同源的 `PATCH /api/admin/users/{用户UUID}` 设置 `disabled` 或 `platform_admin`；普通用户请求被拒绝，管理员不能通过该接口修改自己。变更会撤销目标账号所有会话并记录审计事件。团队管理员无法授予其他管理角色、调整平台额度或创建新账号。

## 更新、备份与回滚

更新前备份 `.env.public`、`.env.gateway`、TLS 证书、业务数据库、LiteLLM 数据库和 Qdrant 快照。环境文件含解密网关密钥所需的 Secret，必须加密保存到服务器外。

```bash
mkdir -p backups
chmod 700 backups
docker compose --env-file .env.public -f docker/compose.public.yaml exec -T postgres \
  pg_dump -U postgres -Fc supportpilot > backups/supportpilot.dump
docker compose --env-file .env.public -f docker/compose.public.yaml exec -T postgres \
  pg_dump -U postgres -Fc litellm > backups/litellm.dump
chmod 600 backups/*.dump
```

Qdrant 快照按 [官方文档](https://qdrant.tech/documentation/concepts/snapshots/)创建并保存。上线前在独立环境演练恢复。数据库迁移可能使旧版本不兼容；回滚镜像前应确认迁移兼容性，必要时恢复完整备份。

升级流程：更新仓库、将 `IMAGE_TAG` 改为已验证提交、运行 `pull` 和 `up`。检查 `/api/health`、登录和一轮 SSE。`down` 保留数据，**不要使用 `down -v`**。

## 排障

- 502：检查后端健康和内部 DNS；`python3 scripts/public_deploy.py logs`。
- 403：检查域名、`PUBLIC_HOST`、`PUBLIC_ORIGIN` 和 HTTPS，禁止使用其他域名或 IP 提交 Cookie 请求。
- 429：可能触发 NGINX、登录限速、用户或全站额度、并发限制。
- 模型预算错误：LiteLLM 可能返回 422；查看内部网关日志及消费记录，不要将原始厂商响应和凭据返回浏览器。
- 注册不显示：检查邮件、人机验证、演示团队及开关配置；后端重建后才读取新环境变量。
- 数据库不可用时恢复服务，禁止切换成无认证模式或直连厂商。

阿里云参考入口：[ECS 文档](https://help.aliyun.com/zh/ecs/)、[安全组](https://help.aliyun.com/zh/ecs/user-guide/security-groups)、[备案](https://help.aliyun.com/zh/icp-filing/)。配置和购买条件以控制台及官方当前文档为准。
