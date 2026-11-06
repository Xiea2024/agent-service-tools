# SupportPilot 运维操作

命令在项目根目录的 PowerShell 执行，使用已有 `.env`。首次部署见 [启动说明](getting-started.md)，指标与告警见 [运行监控](../monitoring/README.md)。

## 任务查询与恢复

```powershell
.\.venv\Scripts\python.exe scripts/show_message_tasks.py --latest --timeline --format table
.\.venv\Scripts\python.exe scripts/show_message_tasks.py --state needs_review --limit 20
.\.venv\Scripts\python.exe scripts/show_message_tasks.py --task-id "任务编号" --timeline
```

可用 `--since` / `--until` 筛选创建时间，无时区的时间按北京时间解释，起点包含、终点不包含。`--preview` 会显示问题摘要，仅在本机排障时使用。事件过多时使用输出中的 `next_after_event_id` 配合 `--after-event-id` 分页。

`needs_review` 表示后端调用结果不明。先停止飞书入口，核对后端日志与对应线程历史，确认旧调用已结束并处理其结果，再解除阻塞：

```powershell
.\.venv\Scripts\python.exe scripts/show_message_tasks.py --task-id "任务编号" --action abandon --backend-checked --reason "已核对旧调用结束并处理结果"
```

此操作记录放弃及审计原因，不删除后端消息，也不重新调用模型。同一聊天有多条待确认任务时逐条核对。

已有答案但发送最终失败的任务，停止飞书后可恢复发送：

```powershell
.\.venv\Scripts\python.exe scripts/show_message_tasks.py --task-id "任务编号" --action retry-send --reason "发送服务已恢复，复用已有答案"
```

仅 `failed` 且已保存回复的任务允许此操作；重启入口后由 Worker 发送。重试同一人工操作可复用 `--operation-id`。不要直接改数据库触发模型重跑。

普通问答、菜单与会话操作按顺序处理；Redis 模式的接收回执和卡片进度由独立通知线程更新。查询中的 `notification` 表示展示状态，绑定卡片编号不等于最终答案展示成功。回答快照先于评价卡片保存，反馈检查操作者归属，首个有效评价生效。

## 升级已有任务数据库

停止飞书入口后运行，备份目录留在已忽略的 `.local/` 内：

```powershell
.\.venv\Scripts\python.exe scripts/migrate_task_db.py --backup-dir .local/backups
```

脚本只升级已有任务库，并使用 Worker 文件锁；自定义路径用 `--db`。备份与升级不包含会话库、checkpoint、反馈库或 Docker 卷，完整数据保留要求见 [停止与保留数据](getting-started.md#9-停止与保留数据)。

## 向量索引冲突

Collection 校验维度、距离、模型和接口配置；相同维度不代表相同向量空间。完全相同输入可幂等重试，已有向量文件可复用，避免再次请求 Embedding：

```powershell
.\.venv\Scripts\python.exe scripts/index_document_chunks.py .local/feishu-documents/demo.vectors.json --reuse-vectors
```

同一文档的正文、版本或切分参数变化时，目前拒绝混写；模型配置不匹配或无法识别的 Collection 也不接管。可另建 Collection 并重新导入所需文档，再让查询使用同一配置。当前没有更新删除同步和跨批事务，中途失败可能已写入部分数据；不要直接删除旧库解决冲突。

## 评测报告发布

```powershell
.\.venv\Scripts\python.exe scripts/show_evaluation_job.py "评测任务编号"
.\.venv\Scripts\python.exe scripts/publish_evaluation_report.py --run-dir "evals/supportpilot/runs/运行编号" --open-id "发起人的open_id"
```

第二条会创建远端文档并向指定用户授予阅读权限，只接受已完成的本地评测。先检查任务的 `document_id` 和目录中的 `publication.json`，避免重复创建；已有发布记录时脚本拒绝覆盖。发布失败可能已创建文档，应先排查该文档，不要直接删除记录重试。发布报告需要创建及写入文档、添加协作者和读取文档元数据等权限，同时核对目标文件夹授权。应用 API 权限和目标文件访问授权分别配置。

## Langfuse 调用追踪

在 `.env` 设置 `LANGFUSE_TRACING=true`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY` 与项目区域对应的 `LANGFUSE_HOST`，保持 `LANGFUSE_CAPTURE_CONTENT=false`，然后重启后端与飞书入口。`LANGFUSE_BASE_URL` 也受支持，若同时配置则优先于 HOST。

```powershell
# 检查项目访问权限；不上传测试记录。
.\.venv\Scripts\python.exe scripts/check_langfuse.py
# 上传合成 trace 并尝试读回；不调用模型。
.\.venv\Scripts\python.exe scripts/check_langfuse.py --send-test
# 索引延迟时按已有编号查询。
.\.venv\Scripts\python.exe scripts/check_langfuse.py --trace-id "32位trace编号"
```

真实问答后，在任务查询输出中取 `context.trace_id`，到 Langfuse 查看 Agent、模型和检索工具节点。`task_id` 关联业务任务，`run_id` 关联后端执行，`thread_id` 关联会话。SDK 初始化、合成记录读回、真实问答收录分别验证；健康接口不证明云端已收到数据。

默认屏蔽问题、答案、Prompt 和检索正文；开启内容采集前需确认材料可上传。Token 以供应商返回值为准，缺少用量或模型价格不表示费用为零。追踪导出失败不替代本地任务状态判断，强制结束进程可能丢失最后一批追踪数据。

## Redis 集成验证

离线测试及固定案例校验见 README。已有本地 Redis 时，可显式启用集成测试：

```powershell
$env:SUPPORTPILOT_TEST_REDIS_URL='redis://127.0.0.1:6379/15'
.\.venv\Scripts\python.exe -m pytest tests/test_feishu_tasks.py -q
Remove-Item Env:SUPPORTPILOT_TEST_REDIS_URL
```

测试使用独立随机命名空间；模型及飞书接口仍为模拟，不能代替真实问答验收。Redis AOF 每秒刷盘存在未落盘窗口，SQLite 台账用于补发；两份存储同时丢失不在恢复保证内。
