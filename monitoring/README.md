# SupportPilot 运行监控

本轮只增加 Prometheus + Grafana，复用当前本机 Python、Redis Streams 和 SQLite 台账。没有新增业务管理网页，没有修改任务恢复策略。Langfuse、Token 与费用统计不属于本轮。

## 启动

在项目根目录执行。保留现有 `.env` 和业务数据库，不要复制模板覆盖它们。

```powershell
uv sync --frozen --group feishu --group rag
```

在 `.env` 修改或添加一次：

```dotenv
METRICS_ENABLED=true
FEISHU_QUEUE_BACKEND=redis
FEISHU_METRICS_HOST=127.0.0.1
FEISHU_METRICS_PORT=9101
```

正常停止并重启 `src/run_service.py` 和 `src/run_feishu.py`，再检查：

```powershell
Invoke-WebRequest http://127.0.0.1:8080/metrics -UseBasicParsing | Select-Object -ExpandProperty StatusCode
Invoke-WebRequest http://127.0.0.1:9101/metrics -UseBasicParsing | Select-Object -ExpandProperty StatusCode
docker compose -f monitoring/compose.yaml up -d
```

打开 [Grafana](http://localhost:3000)，首次默认账户为 `admin / admin`，首次登录立即设置自己的密码。数据源、SupportPilot 文件夹、运行看板和七条告警规则自动创建，无须手动导入 JSON。打开 [运行看板](http://localhost:3000/d/supportpilot-overview) 和 [Prometheus targets](http://localhost:9090/targets)。首次采集约需 15 秒，速率和 P95 至少需要两次采样；没有调用时显示无数据。

两个监控服务仅发布到宿主机回环端口，持久卷保留账户和监控历史，Prometheus 保留 15 天。不要执行 `down -v`；日常停止使用：

```powershell
docker compose -f monitoring/compose.yaml stop
```

## 网络与访问边界

默认适配 Windows Docker Desktop，容器通过 `host.docker.internal:8080` 和 `:9101` 抓取宿主 Python 指标，不使用容器自己的 localhost。

如果本机访问指标正常，但 targets 报连接拒绝，先检查 Docker Desktop 到宿主网络、Windows 防火墙和端口。若需要监听所有接口，将后端 `HOST=0.0.0.0`、飞书 `FEISHU_METRICS_HOST=0.0.0.0`，重启两进程，并通过防火墙将 8080/9101 限定给可信本机/Docker 网络。后端应配置已有 `AUTH_SECRET`。指标端点不要求 Bearer，但不包含消息正文、用户 ID、任务 ID 或凭据。

Linux Docker Engine 的 host-gateway 不能访问仅监听宿主回环的服务，需要上述受限网络监听。端口变更需同时修改 `prometheus/prometheus.yml`；修改后重启 Prometheus。

## 看板口径

| 数据 | 含义与限制 |
| --- | --- |
| 采集目标在线 | 指标端点可达，不证明飞书长连接、模型或全部依赖可用 |
| 当前任务状态 | 只统计当前应用命名空间的普通问答；不把菜单、反馈、评测发起算作问答 |
| 最近一小时交付成功率 | 按当前结果最后更新时间，成功数 / 成功、失败、待确认总数；不是模型正确率；人工修改状态会改变窗口归属 |
| 排队 / 执行 / 待发送 | 对应 queued / invoking / ready；needs_review、blocked 单列 |
| 最长任务年龄 | 从创建时间到现在，包含排队、模型执行和发送等待 |
| 平均端到端耗时 | 最近一小时更新为成功的任务，创建到最后更新时间的平均值，含中断恢复与发送重试；没有成功样本时不填零 |
| 模型 / 检索次数与 P95 | 当前业务 Agent 的模型调用和检索工具；模型超时、检索错误和无结果单列；耗时包含 SDK 内部重试 |
| 失败分类 | unknown_execution、prior_task_unresolved、delivery、backend_unavailable_or_rejected、other；不把任意异常文本用作标签 |
| Worker 最近轮询 | 成功访问 Redis 与任务台账的时间；长模型调用期间增长，180 秒未轮询触发告警 |

任务 gauges 从 SQLite 读取，重启保留；操作 counters/histograms 是进程内指标，进程重启归零，Prometheus 的 rate/increase 按 counter reset 处理。停机期间没有采集到的操作无法补造。普通 API 请求计时不等于 SSE 流完整执行时间；Agent 操作指标用于检查模型/检索耗时。

## 告警

Grafana → Alerting → Alert rules：指标目标不可达、Worker 停止轮询、任务数据库不可读、存在待确认任务、任务超过 180 秒、最近一小时有失败、模型/检索异常。阈值为初始运行设置，可在文件中修改后重建 Grafana。

本轮规则在 Grafana 中评估和显示，**尚未配置外部通知接收端**。需要在 Contact points 配置接收渠道，并在 Notification policies 将 `service=supportpilot` 路由到该渠道，然后实际测试。通用 Grafana Webhook 不能假定兼容飞书机器人消息格式；飞书通知适配单独接入，不把“规则触发”写成“飞书通知送达”。

目标不可达使用 Prometheus `up` 判断；其他指标缺失时不伪造故障数据，由目标不可达告警覆盖。数据库不可读单独输出 `supportpilot_task_store_up=0`。

## 验收

1. 两个 `/metrics` 返回 200，Prometheus targets 两项均为 UP。
2. 发一条真实飞书问题，确认队列/执行状态变化、成功任务增加，模型/检索指标更新。
3. 正常停止后端，保持飞书入口运行，再发合成问题：台账失败，Grafana 失败数量变化，目标不可达持续一分钟后触发。重启后端后目标恢复。
4. 发送工具检索问题，确认检索指标；服务错误不能仅因兜底回答已发出而显示为工具成功。
5. 重启飞书，确认任务状态数量保留；查看 Worker 轮询恢复，无新增重复回答。
6. 查询任务详情仍用 `scripts/show_message_tasks.py`；按现有 Redis 恢复说明处理 `needs_review`，不因告警直接重跑。

自动测试（只使用合成任务和模拟模型，不调用真实飞书）：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/observability tests/test_feishu_tasks.py tests/test_feishu_progress.py tests/agents/test_dev_issue_rag.py tests/test_document_retrieval.py -q
```

## 文件结构

```text
src/observability/           指标定义、API 接入、只读台账 collector
tests/observability/         指标语义、隔离、脱敏及 HTTP 测试
monitoring/compose.yaml      两个独立监控容器
monitoring/prometheus/       抓取配置
monitoring/grafana/          数据源、看板和告警的版本化配置
```

任务流水与 Langfuse 后续单独接入，不在本步骤中增加其他平台。
