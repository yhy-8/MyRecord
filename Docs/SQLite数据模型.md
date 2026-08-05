# SQLite 数据模型

分析数据库默认位于 `AnalysisReports/.analysis.sqlite3`。它是可删除重建的运行缓存，不替代 Markdown 日记或报告。

## 1. 职责边界

SQLite 只承担两项职责：

- 保存周报和月报运行生命周期，界定失败重试缓存；
- 保存 Agent 阶段产物，使相同输入的失败重试能够复用已验证阶段，减少等待和 Token。

来源索引直接从本次日记输入写入报告，不再持久化。模型、生成耗时和本次 Token 用量也直接写入报告文件头。

## 2. 连接与事务

每个存储操作创建独立连接，并启用：

- `PRAGMA foreign_keys = ON`
- `PRAGMA busy_timeout = 10000`
- `PRAGMA journal_mode = WAL`
- 写事务使用 `BEGIN IMMEDIATE`

事务只覆盖 SQL，不跨越模型或网络请求。普通日记记录不访问 SQLite。

## 3. analysis_runs

一行表示一次周报或月报运行。

| 字段 | 含义 |
|---|---|
| `id` | 32 位 UUID 十六进制运行 ID |
| `kind` | `weekly` 或 `monthly` |
| `period_start/end` | 分析闭区间日期 |
| `origin` | `manual` 或 `auto` |
| `trigger` | `manual`、`scheduled` 或 `retry` |
| `model_name` | 实际请求使用的模型标识 |
| `status` | `running`、`completed`、`failed` |
| `input_hash` | 完整输入快照 SHA-256 |
| `report_path` | 成功交付文件路径 |
| `error` | 失败原因 |
| `created_at/completed_at` | 本地时间戳 |

手动触发只允许 `origin=manual, trigger=manual`。计划任务与重试使用自动来源。同周期重跑插入新行，固定 Markdown 路径可以被覆盖。

## 4. agent_artifacts

该表保存一次运行内各 Agent 与中控检索阶段的 JSON 产物。模型直接返回的只是最小标量对象；主题数组、来源绑定、Markdown 和搜索证据等结构由中控生成后写入缓存。

| 字段 | 含义 |
|---|---|
| `id` | 产物 ID |
| `run_id` | 所属运行 |
| `agent` | Agent 或 `research_search` |
| `revision` | 同运行、同 Agent 从 1 递增的版本号 |
| `status` | `completed` 或 `failed` |
| `payload_json` | 结构化载荷和遥测 |
| `error` | 解析、校验、审查或调用错误 |
| `created_at` | 创建时间 |

唯一约束为 `(run_id, agent, revision)`。`payload_json._telemetry` 保存请求、Token、缓存 Token、`finish_reasons`、空正文补答次数和搜索遥测。周报的 `research_search` 保存 `W-*` 证据；月报不会产生检索产物。

报告文件头的 Token 总数在运行内存中按每次实际模型调用累计，避免把同一遥测在不同阶段产物中重复计算。缓存复用阶段没有新模型调用，因此不计入本次报告用量。

## 5. 删除重建

程序不迁移旧数据库结构。结构不匹配时只报错，不覆盖或修改原文件。确认没有报告正在运行后，删除以下文件即可重建：

```text
AnalysisReports/.analysis.sqlite3
AnalysisReports/.analysis.sqlite3-wal
AnalysisReports/.analysis.sqlite3-shm
```

删除会清空旧运行审计和失败重试缓存，但不会影响：

- `Records/` 原始日记；
- 已生成的周报和月报正文；
- `.automation-state.json` 中的自动任务目标与失败状态；
- `config.yaml` 中的模型和密钥。

数据库错误不得触发对日记或报告的清理。
