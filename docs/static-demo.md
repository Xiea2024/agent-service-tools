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

## 使用 GitHub Pages 发布

GitHub Pages 支持公开仓库在 GitHub Free 下托管静态页面，提供 `github.io` 地址，详见 [GitHub 官方说明](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages)。不需要购买服务器、域名或消耗模型额度；托管与 Actions 使用仍受平台配额和规则约束。

1. 将本次代码推送至 GitHub。
2. 在仓库 **Settings → Pages → Build and deployment → Source** 选择 **GitHub Actions**。
3. 在 **Actions → Publish static demo → Run workflow** 手动运行。
4. 成功后打开部署任务输出的 HTTPS 地址。默认项目地址通常为 `https://xiea2024.github.io/agent-service-tools/`；以任务实际输出为准。

工作流只构建并发布 `dist-demo`，不部署后端、不使用模型密钥，不依赖 Docker 或 PostgreSQL。首次发布若提示 Pages 未启用，先完成第 2 步。更新时再次运行工作流。原有 CI 也会上传 `static-demo` 构建产物，但不会自动发布 Pages。

一个仓库只有一个 Pages 站点；如果此仓库已有 Pages 内容，运行发布工作流会替换站点内容。发布前确认目标仓库和站点符合预期。也可以将 `dist-demo/` 上传到其他静态托管平台。

参考：[GitHub Pages 自定义工作流](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)。
