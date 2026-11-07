# 个人导航首页、博客与 SupportPilot

React + TypeScript + Vite 共用一个站点：

| 地址 | 页面 |
| --- | --- |
| `/` | 个人导航首页，卡通星球展开最近 5 篇已发布文章，链接到博客和工作台 |
| `/supportpilot` | 原 SupportPilot 工作台 |
| `/blog` | 博客列表，支持切换文章 / 草稿管理 |
| `/blog/new` | Markdown 写作、预览、保存草稿和发布 |
| `/blog/:id` | 阅读文章 |
| `/blog/:id/edit` | 编辑、撤回发布和删除文章 |

桌面端悬停星球圆心展开，鼠标在整个星球区域内移动保持展开，离开后收起。键盘 Tab 也能展开并进入文章链接。手机端保留「全部文章」入口，在星球下方直接列出最近文章。没有文章时显示空状态，不插入示例内容。

GitHub 主页集中配置在 `src/blog-api.ts` 的 `githubUrl`，当前为仓库所属账号 `Xiea2024`。站点标识在 `src/Portal.tsx` 中修改。径向展开交互参考 [RadialMenu-Extended](https://codepen.io/hundertj/pen/YxwmpJ)，使用 React 状态与 CSS 独立实现。

## 博客数据

启动后端时自动创建 `.local/blog.sqlite3` 和 `blog_posts` 表；也会在首次访问博客接口时建表。可以用后端环境变量 `BLOG_DB_PATH` 指定数据库路径。文章保存在服务器 SQLite 中，刷新网页或重启服务后仍然保留。备份此数据库即可备份文章。

字段：`id`、`title`、`summary`、`content`（Markdown）、`published`、`created_at`、`updated_at`、`published_at`。发布列表按首次发布时间倒序排列，草稿不会出现在首页。没有预填文章。

后端接口（通过 Vite 代理时增加 `/api` 前缀）：

| 方法 | 地址 | 功能 |
| --- | --- | --- |
| GET | `/blog/posts?limit=5` | 最近已发布文章 |
| GET | `/blog/posts?drafts=true` | 文章与草稿，最多 100 篇 |
| GET | `/blog/posts/:id` | 获取正文 |
| POST | `/blog/posts` | 新建文章 |
| PUT | `/blog/posts/:id` | 修改文章 |
| DELETE | `/blog/posts/:id` | 永久删除文章 |

POST / PUT 的 JSON 字段为 `title`、`summary`、`content`、`published`。所有博客接口复用现有后端 Bearer 认证和本机 Vite 服务端代理，当前适用于个人本地写作。公网部署前需为写作和草稿管理补充独立的管理员登录与权限校验；不能直接公开自动附带共享密钥的代理。静态托管需要将 `/blog/*`、`/supportpilot` 等前端路由回退到 `index.html`，并配置后端 `/api` 代理。

博客后端验证：在项目根目录执行 `.\.venv\Scripts\python.exe -m pytest tests/unit/test_blog.py -q`。

## SupportPilot 工作台

React + TypeScript + Vite 页面，复用现有 FastAPI `dev-issue-agent`，支持提问、流式回答、历史会话与文档证据。网页会话与飞书会话独立，未加入 MCP 或语音功能。

## 本地启动

需要 Node.js 22.12+（推荐当前 Node.js 24 LTS）、npm 和已配置好的 Python 后端。命令均为 PowerShell。

终端一，在项目根目录启动后端：

```powershell
.\.venv\Scripts\python.exe src/run_service.py
```

终端二，在项目根目录执行：

```powershell
cd frontend
npm ci
npm run dev
```

打开 <http://127.0.0.1:5173> 进入导航首页，<http://127.0.0.1:5173/supportpilot> 进入工作台。网页服务默认仅监听本机。

代理在 Node 进程读取根目录 `.env` 中的 `AUTH_SECRET`，自动添加后端认证头。密钥不会通过 `VITE_` 变量传给浏览器；更改配置后需重启网页服务。后端默认地址为 `http://127.0.0.1:8080`，端口读取根目录 `PORT`。

临时指定其他后端地址：

```powershell
$env:WEB_API_TARGET = 'http://127.0.0.1:8081'
npm run dev
```

后端需要注册 `dev-issue-agent`，并配置支持工具调用的聊天模型。证据来自已导入 Qdrant 的文档；没有命中或调用失败会显示实际状态，不会生成演示证据。

## 使用

- 输入问题，Enter 发送，Shift + Enter 换行；中文输入法确认候选字不会触发发送。
- 页面先显示 token，再由完整 AI 消息替换当前草稿，工具消息保存在当前轮次。
- 左侧从 `/threads` 加载最近最多 100 个会话，可搜索、点击恢复并继续追问。
- 右侧显示当前轮次的工具参数、返回状态、文档片段和原文链接。点击某轮回答的工具记录可以切换证据。
- “刷新历史”从 checkpoint 重新加载。停止接收或网络中断不会保证后端模型停止，先刷新历史确认结果，避免重复提交。
- 浏览器仅在 localStorage 保存匿名用户编号，问题和回答由后端 checkpoint 保存。清理浏览器数据会生成新身份。

## 验证与构建

```powershell
npm test
npm run build
npm run preview
```

构建产物在 `frontend/dist/`，预览地址为 <http://127.0.0.1:4173>，预览服务也使用相同代理。测试覆盖 UTF-8 分包、SSE 终止、消息去重、工具结果、历史恢复、继续会话及失败展示。

## 代码阅读顺序

| 文件 | 职责 |
| --- | --- |
| `src/api.ts` | HTTP 请求、SSE 解析与后端数据类型 |
| `src/conversation.ts` | 按轮次恢复历史、关联工具调用与结果、提取和去重证据 |
| `src/App.tsx` | 会话列表、聊天流、证据面板、加载与错误状态 |
| `src/styles.css` | 响应式三栏布局；窄屏会话列表与证据抽屉 |
| `vite.config.ts` | 本地代理与服务端凭据读取 |

## 部署边界

当前交付为本地工作台，不包含公网托管或登录系统。浏览器编号只用于定位会话，不是鉴权凭证；现有后端共享 Bearer 也不提供逐用户会话权限隔离。

生产环境需要由可信服务端代理 `/api`，保留流式响应、关闭代理缓冲并配置足够长的超时；直接托管 `dist` 不会包含 Vite 的代理。开放给其他用户前，需要补充登录与逐会话归属校验，不能直接公开附带共享密钥的本地代理。
