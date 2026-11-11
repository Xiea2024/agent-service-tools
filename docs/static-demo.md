# 零后端静态演示

用于招聘展示界面与问答交互，不需要阿里云、数据库、模型密钥或邮件服务。阿里云和真实后端部署文件继续保留。

## 体验内容

- 使用 `guest / guest` 进入演示。
- 三个预录场景：接口 500、依赖升级后启动失败、跨域与 Cookie。
- 模拟流式回答、工具调用记录、知识库片段、可打开的静态来源页。
- 历史会话、新建会话、搜索、复制、反馈、停止输出。
- 完成一个场景后，输入“生成交接报告”继续对应的预录流程。
- 非预设问题会提示选择演示场景，不伪装成实时 AI 推理。

`guest / guest` 是公开的前端体验入口，不提供安全认证。演示素材和代码均可被访客读取。不要放入真实凭据、内部文档或客户资料。会话与反馈仅保存于当前标签页的 sessionStorage；存储禁用时使用内存；重置演示可恢复三个示例，退出不会删除本标签页的演示历史。

## 本地启动与构建

```bash
cd frontend
npm ci
npm run dev:demo
```

打开终端给出的地址（默认 `http://127.0.0.1:5173`），不需要启动后端。

```bash
npm run build:demo
npm run preview:demo
```

静态产物位于 `frontend/dist-demo/`，通过任意 HTTP 静态服务即可访问。不要直接双击 HTML，浏览器会限制 ES 模块的 file:// 加载。构建使用相对资源路径，支持部署到 GitHub Pages 的仓库子路径。

普通 `npm run build` 和原有部署保持真实服务模式；只有 `--mode demo` 启用静态传输层。演示模式禁用 Vite API 代理，问答流程不请求 `/api`、不连接模型；点击来源页只访问同站点静态 HTML。静态托管平台仍可能记录正常访问日志。

## 使用 Render 发布（当前推荐）

仓库根目录的 `render.yaml` 只定义一个 Static Site，不会创建后端、数据库、Docker 服务或模型网关。站点自动使用 Render 提供的 HTTPS 子域名；无需修改 Vite `allowedHosts`，也不需要 Cloudflared。

### 首次登录和仓库授权

1. 打开 [Render 控制台](https://dashboard.render.com/)，使用自己的账号登录或注册。密码、验证码和 API Key 不要放入仓库或聊天记录。
2. 关联 GitHub 时，若需要安装 Render GitHub App，选择只授权 `Xiea2024/agent-service-tools` 仓库。
3. 若通过助手部署，完成当前会话中的 Render 连接授权；没有连接也可以按下述控制台步骤创建站点。

### 方式一：从 Blueprint 创建

代码推送后，在 Render 控制台选择 **New → Blueprint**，连接本仓库的 `main` 分支，使用根目录 `render.yaml`。部署预览应只有一个名为 `supportpilot-static-demo` 的静态站点，没有付费计算实例或数据库，再应用配置。

`type: web` 配合 `runtime: static` 在 Blueprint 中表示静态站点，不是 Node Web Service。静态站点不设置计算实例 `plan` 或 `startCommand`。

### 方式二：直接创建 Static Site

在控制台选择 **New → Static Site**，连接本仓库，按下表填写：

| 配置 | 值 |
| --- | --- |
| Name | `supportpilot-static-demo`（若重名可调整） |
| Branch | `main` |
| Root Directory | `frontend` |
| Build Command | `npm ci && npm run build:demo` |
| Publish Directory | `dist-demo` |
| Environment | `NODE_VERSION=24`、`SKIP_INSTALL_DEPS=true` |
| Auto Deploy | On Commit |

Publish Directory 相对 `frontend`，不要再填成 `frontend/dist-demo`。无需配置模型密钥、邮件密钥、端口、启动命令或 API 代理。

此演示只使用根页面和真实存在的静态引用页，暂不配置 `/* → /index.html` 通配重写。完成部署后，使用控制台返回的真实 `https://...onrender.com` 地址，不要根据服务名称猜测域名。

### 发布验收与更新

- 使用 `guest / guest` 进入演示，打开三个历史案例。
- 新建问答，检查模拟流式输出、右侧来源、原文页面和反馈。
- 在一个案例后输入“生成交接报告”，检查预录的后续流程。
- 刷新页面确认会话恢复；在手机上检查输入框和证据面板。
- 浏览器 Network 中不应出现 `/api` 或模型厂商请求，静态资源与来源页应加载成功。

后续推送 `main` 下的前端修改会触发自动构建。自动部署由 Render 直接跟踪 GitHub；不需要 GitHub Actions SSH 登录或 Render API Key。`rootDir: frontend` 让纯后端代码变更不触发普通自动构建；Blueprint 文件变更仍会被 Render 处理。

Render 静态托管免费，带宽与构建分钟仍受工作区套餐额度限制；只使用静态站点，不升级付费套餐或开通额外计算资源。参见 [Static Sites](https://render.com/docs/static-sites)、[Monorepo 根目录规则](https://render.com/docs/monorepo-support)、[Blueprint 配置](https://render.com/docs/blueprint-spec)。

## 使用 GitHub Pages 发布（可选）

GitHub Pages 支持公开仓库在 GitHub Free 下托管静态页面，提供 `github.io` 地址，详见 [GitHub 官方说明](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages)。不需要购买服务器、域名或消耗模型额度；托管与 Actions 使用仍受平台配额和规则约束。

1. 将本次代码推送至 GitHub。
2. 在仓库 **Settings → Pages → Build and deployment → Source** 选择 **GitHub Actions**。
3. 在 **Actions → Publish static demo → Run workflow** 手动运行。
4. 成功后打开部署任务输出的 HTTPS 地址。默认项目地址通常为 `https://xiea2024.github.io/agent-service-tools/`；以任务实际输出为准。

工作流只构建并发布 `dist-demo`，不部署后端、不使用模型密钥，不依赖 Docker 或 PostgreSQL。首次发布若提示 Pages 未启用，先完成第 2 步。更新时再次运行工作流。原有 CI 也会上传 `static-demo` 构建产物，但不会自动发布 Pages。

一个仓库只有一个 Pages 站点；如果此仓库已有 Pages 内容，运行发布工作流会替换站点内容。发布前确认目标仓库和站点符合预期。也可以将 `dist-demo/` 上传到其他静态托管平台。

参考：[GitHub Pages 自定义工作流](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)。
