# 一键启动与 GitHub Actions / GHCR

公网版本使用独立 `backend-public` 镜像，增加真实 LiteLLM 与完整 HTTPS 栈联调、运行时依赖审计门禁。操作步骤见[阿里云部署](aliyun-deployment.md)；下文的 `.env.docker` 与 `scripts/start.py` 用于本地团队工作台。

交付链路：Ruff、格式和类型检查 → pytest（真实 PostgreSQL / Redis）与前端测试构建 → 构建后端、网页镜像 → 容器健康检查 → 发布 GHCR。GitHub Actions 负责检查和发布镜像，运行服务仍需要 Docker 主机。

## 1. 本地一键启动

需要 Python 3 和已经启动的 Docker（含 Compose v2）；本机不需要安装 Node.js、uv 或项目 Python 依赖。

Windows PowerShell：

```powershell
.\start.ps1 --demo --admin-email admin@example.com --team "研发团队"
```

跨平台入口：

```powershell
python scripts/start.py --demo --admin-email admin@example.com
```

Linux / macOS：

```bash
sh start.sh --demo --admin-email admin@example.com
```

首次构建镜像并创建独立 `.env.docker`、PostgreSQL、Qdrant、后端和 Nginx 网页服务。管理员密码交互输入，至少 10 字符，不写进命令历史。默认打开 `http://127.0.0.1:8088/supportpilot`，可通过 `.env.docker` 的 `WEB_PORT` 修改端口。

`--admin-email` 用于创建新账号，已有账号不应重复创建；以后启动省略该参数。`--demo` 仅在首次创建配置时使用假模型，从公开模板生成配置，不复制真实凭据。已有配置不会覆盖。正常首次启动若存在 `.env`，会复制模型配置，并为这一独立栈生成数据库配置；原 `.env` 不变。没有 `.env` 时默认使用假模型。

使用真实模型时修改 `.env.docker` 的 Provider 配置、`USE_FAKE_MODEL=false` 和 `DEFAULT_MODEL`，然后再次启动。Embedding 仍需独立配置，文档导入见[多租户说明](multi-tenancy.md)。

```powershell
# 正常启动 / 应用配置变更
python scripts/start.py
# 跟踪日志；Ctrl+C 退出日志但保留服务
python scripts/start.py --logs
# 停止容器并保留数据卷
python scripts/start.py --stop
# 只生成配置，不启动；已有配置不覆盖
python scripts/start.py --configure-only --demo
```

`--env-file` 可指定独立配置，`COMPOSE_PROJECT_NAME` 可指定独立项目。默认命名卷为 `supportpilot-app_postgres_data`、`supportpilot-app_qdrant_data`、`supportpilot-app_app_data`。已有飞书/SQLite 目录和其他 Compose 卷不迁移、不改写。默认只向本机暴露网页端口；此编排明确启用团队模式，旧飞书和博客入口不在此部署范围内。

## 2. GitHub Actions 检查内容

工作流：[ci.yml](../.github/workflows/ci.yml)。监听 push、pull request 和手动运行；更新同一分支时取消旧流水线。

| Job | 检查与产物 |
| --- | --- |
| `lint` | actionlint 检查工作流与 Shell；Ruff 规则、Ruff 格式、Pyrefly 类型检查 |
| `pytest` | Python 3.13；锁定 dev / feishu / rag 依赖；真实 PostgreSQL 17、Redis 7.4；上传 JUnit、覆盖率 XML |
| `frontend` | Node.js 24、`npm ci`、Vitest、TypeScript 与 Vite 构建；上传 `frontend-dist` |
| `images` | 等待上述三个 Job 成功；构建 backend / web 镜像，使用假模型做容器健康检查，再按规则发布 |

Python 统一使用 `uv==0.13.5`、`uv sync --frozen`，不修改锁文件。测试使用假凭据、模拟模型或内置假模型，不需要 OpenAI、飞书或 Langfuse 的生产密钥。Redis 测试通过 `SUPPORTPILOT_TEST_REDIS_URL` 启用，PostgreSQL 测试通过 `TEST_POSTGRES_DSN` 启用。

默认不运行 `docker` 标记的旧 Compose 端到端测试，它们针对原 Streamlit 部署；新镜像在 `images` Job 单独构建并启动检查。没有设置任意覆盖率阈值，报告可用于后续建立基线。测试报告保留 14 天。

本地对应检查：

```powershell
uvx --from uv==0.13.5 uv sync --frozen --group feishu --group rag
uv run --no-sync ruff check src tests scripts
uv run --no-sync ruff format --check src tests scripts
uv run --no-sync pyrefly check
uv run --no-sync pytest tests -q --strict-markers -m "not docker"
cd frontend
npm ci
npm test
npm run build
```

本地未指定测试数据库连接时，相应真实基础设施测试会跳过；Actions 中已经配置服务和连接。

## 3. 发布到 GHCR

镜像名称自动根据仓库名生成并转换为小写。假设仓库为 `YourName/agent-service-tools`：

```text
ghcr.io/yourname/agent-service-tools-backend:latest
ghcr.io/yourname/agent-service-tools-web:latest
```

发布规则：

- PR、普通功能分支：检查、构建和健康检查，不登录 GHCR、不推送。
- 默认分支：检查成功后发布 `latest`、`sha-<完整提交 SHA>`。
- 推送 `v*` 标签：检查成功后发布标签名和 SHA；有效语义版本同时发布去掉 `v` 的版本号，例如 `v1.0.0`、`1.0.0`。
- 默认分支手动运行也可发布；其他分支手动运行只验证。

工作流使用 GitHub 自动提供的 `GITHUB_TOKEN` 和镜像 Job 的 `packages: write` 权限，仓库无需保存个人 PAT。实现参考 [GitHub 官方镜像发布说明](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images)。

上传代码后：

1. 在仓库 Actions 页面启用工作流。
2. 确认仓库/组织策略允许 Actions 和发布 Packages；若限制第三方 Actions，需要允许此工作流列出的 actions。
3. 推送默认分支或提交 PR，观察四类 Job。
4. 发布成功后在账号/组织 Packages 页面检查两个镜像。公开仓库不代表镜像一定公开；需要免登录拉取时，单独检查 Package 可见性。

已有同名 GHCR 包时，还需确保包关联此仓库并授予该仓库 Actions 写入权限。镜像包含 OCI source 标签，便于追踪源仓库。

`.env`、`.env.*`、本地数据库、私有文档、缓存、`node_modules` 不进入镜像构建上下文；模型密钥通过运行时配置提供。

## 4. 从 GHCR 启动

在主机拉取代码以取得脚本、Compose 文件，先生成配置：

```bash
python3 scripts/start.py --configure-only --demo
```

编辑 `.env.docker`，保留已生成的 PostgreSQL 用户、密码和库名，添加：

```dotenv
SUPPORTPILOT_IMAGE_PREFIX=ghcr.io/yourname/agent-service-tools
IMAGE_TAG=latest
```

生产更新可将 `IMAGE_TAG` 固定为已验证的 `sha-...` 或版本号。然后：

```bash
python3 scripts/start.py --pull --admin-email admin@example.com
# 已有管理员时
python3 scripts/start.py --pull
```

私有包需要先在主机 `docker login ghcr.io`，使用具备包读取权限的凭据；工作流的临时 `GITHUB_TOKEN` 不用于服务器长期登录。流水线完成镜像交付，不包含 SSH 自动更新服务器。

默认 HTTP 仅绑定本机。公网部署请使用自己的 HTTPS 反向代理，配置 `TENANT_COOKIE_SECURE=true`、实际域名对应的 `TENANT_ALLOWED_ORIGINS`，并完成登录限流、数据库备份和恢复配置。首次初始化数据库后，单独修改环境文件不会同步修改已有 PostgreSQL 用户的密码。
