# 阶段归因：把“哪一步不准”变成可查询的数据

本文说明 Iter 1 落地的阶段观测。目标不是增加功能，而是让一次回答出错时，
能按流水线顺序指出**第一个**出问题的步骤，而不是在日志里翻找。

## 1. 六个阶段

| 阶段 | 记录时机 | 关键决策字段 | 归因含义 |
| --- | --- | --- | --- |
| `route` | supervisor 委派后 | `target`、`decided_by`、`model` | 委派错了专家，或该检索的没检索 |
| `memory` | 预留 | — | 长期记忆召回（后续迭代接入，本版不产生记录） |
| `retrieve` | 每次工具调用 | `hits`、`top_score`、`error_code`、`query_chars` | 检索失败、无命中、没命中应命中的文档 |
| `evidence` | 收尾回答生成后 | `hits`、`cited` | 有命中但没被引用 |
| `generate` | 每次模型调用 | `branch`、`model`、`tool_calls`、`chars` | 超时、预算耗尽、没有产出可用回答 |
| `verify` | 每轮结束 | `answer_chars`、`unsupported_urls` | 引用了没有出处的链接 |

阶段名、结果枚举和指标标签都在 [`src/observability/stages.py`](../src/observability/stages.py) 中固定；
新增阶段必须改那里的枚举，避免调用点拼错后指标静默分裂。

## 2. 记录里有什么、没有什么

只记录**低基数、无正文**的字段：

- 有：阶段名、结果枚举、耗时、变体标识、命中数、分数、字符数、错误码。
- 没有：用户问题、模型回答、文档正文、片段 ID、URL、密钥。

决策字段会经过白名单过滤：键必须匹配 `[a-z][a-z0-9_]{0,31}`，字符串截断到 64 字符，
嵌套结构和 NaN 直接丢弃。因此这份记录可以给评测器、前端和排障人员读取。

## 3. 三个使用入口

同一份记录被三处消费，名字完全一致，这是归因能成立的前提：

1. **指标**：`supportpilot_agent_stage_total{stage,outcome,variant}` 与
   `supportpilot_agent_stage_duration_seconds{stage,variant}`。看板上的漏斗图直接用它们。
2. **接口**：`GET /runs/{run_id}/stages` 返回该轮记录，不含正文。
3. **评测**：`results.jsonl` 的每轮结果新增 `stages` 与 `attribution` 字段，
   `summary.json` 新增 `stage_failures`；`report.md` 增加「失败归因」小节。

## 4. 归因规则

按流水线顺序取第一个失败点，规则在 [`src/harness/attribution.py`](../src/harness/attribution.py)：

```text
route     案例要求检索但没有工具调用 / 要求不检索却调用了工具
retrieve  检索报错或超时 / 结果是 error / 没命中预期文档 / 要求检索却零命中
evidence  有命中但回答没引用 / 回答里有没出处的链接
generate  模型超时或预算耗尽 / 没有产出可用回答 / 执行错误
verify    run_id 不一致、工具请求与结果不配对、轮次超限、工具白名单外、工具返回结构非法
其它      规则都无法解释时记为 unattributed，并列出失败检查名
```

两条原则：

- **不调用模型判断**。输入是评测器已算出的 checks、工具证据和阶段记录，因此同一份输入
  永远得到同一个归因，可以放进 CI 比较。
- **解释不了就明说**。`unattributed` 会保留失败检查名，用来提醒“需要新增一条规则或人工复核”，
  而不是硬塞一个最像的阶段。

## 5. Supervisor 与两个专家

图里有三个 Agent，共用一个 checkpointer 和一套阶段埋点：

```text
START → prepare → supervisor ─┬→ doc_expert ⇄ tools   (检索 + 回答)
                              └→ log_expert           (log_facts 解析 + 单次解释)
```

supervisor 是一个 LLM：它调用 `delegate` 工具并只返回一个 `Command(goto=...)`。
三点设计必须记住，否则会踩坑：

| 决定 | 原因 |
| --- | --- |
| 委派消息**不写入** `messages` | 否则它带着 tool_calls 进 SSE 流，评测的 `known_tools` 和 `tool_budget` 会把每轮判失败 |
| 专家是同图的具名节点，**不是编译子图** | 子图在 SSE 里会同时产生内部事件和父图回显，同一条消息投递两次，`tool_calls_matched` 判失败 |
| 失败必须回退并记录 | 模型超时 → `decided_by=timeout`；目标不在白名单或没调工具 → `decided_by=rules_fallback`，都退回 `doc_expert`，请求不会丢 |

`decided_by` 的取值：`model`（模型给出合法委派）、`rules_fallback`（模型没决定，按
确定性规则兜底）、`timeout`（委派超时）、`default`（未进入委派分支）。

因此“委派错了专家”可以单独被归因：案例用 `expected_intent` 声明期望的专家，
目标不一致就判 `route` 失败，并在理由里带上实际的 `decided_by`。

## 6. 局限

- 记录保存在服务进程内存中，最多保留最近 200 次运行、每次 12 条记录。进程重启、
  多副本部署或记录被挤出后读到空列表，属于预期行为，不影响主链路。
- `evidence`、`verify` 是服务端的粗判；**权威判定仍在评测器**
  （`evaluation.check_turn` 还校验工具返回结构、`run_id` 一致性和轮次上限）。
- 阶段记录不包含 token 用量与成本；这两项在 Iter 3 随 `llm_tokens_total` 一起补。
- `AGENT_VARIANT` 目前只从配置读取。请求方不能注入，避免标签基数被外部控制；
  受控的 A/B 分流在 Iter 3 接入。
