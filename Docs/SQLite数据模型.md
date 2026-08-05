# SQLite 数据模型

分析数据库默认位于 `AnalysisReports/.analysis.sqlite3`。它只保存可重建的报告运行审计、Agent 产物/阶段缓存和记录来源目录，不替代 Markdown 日记或报告。

## 1. 设计目标

SQLite 当前只承担：

- 审计周报和月报运行；
- 保存 Agent 成功或失败产物、请求遥测及安全阶段缓存；
- 保存周报搜索查询、结果和实际使用的证据；
- 将运行中的 `R-*` 映射到日记位置。

新运行不再创建 `daily_profile` 或 `daily_information`。旧数据库中的这两类审计行可以保留，不影响读取。

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
| `kind` | 新运行只允许 `weekly` 或 `monthly` |
| `period_start/end` | 分析闭区间日期 |
| `origin` | `manual` 或 `auto` |
| `trigger` | `manual`、`scheduled` 或 `retry` |
| `model_name` | 模型显示名 |
| `status` | `running`、`completed`、`failed` |
| `input_hash` | 完整输入快照 SHA-256 |
| `report_path` | 成功交付文件路径 |
| `error` | 失败原因 |
| `created_at/completed_at` | 本地时间戳 |

手动触发只允许 `origin=manual, trigger=manual`。计划任务与重试使用自动来源。同周期重跑会插入新行，固定 Markdown 路径可以被覆盖。

## 4. agent_artifacts

该表保存一次运行内各 Agent 与中控阶段的 JSON 产物。

| 字段 | 含义 |
|---|---|
| `id` | 产物 ID |
| `run_id` | 所属运行 |
| `agent` | Agent 或中控检索阶段名 |
| `revision` | 同运行、同 Agent 从 1 递增的版本号 |
| `status` | `completed` 或 `failed` |
| `payload_json` | 结构化载荷 |
| `error` | 解析、校验、审查或调用错误 |
| `created_at` | 创建时间 |

唯一约束为 `(run_id, agent, revision)`。`payload_json._telemetry` 保存请求、token、缓存 token、`finish_reasons`、空正文补答次数和搜索遥测。周报的 `research_search` 保存 `W-*` 证据；月报不会产生检索产物。

## 5. source_catalog 与 run_sources

`source_catalog` 以稳定 `R-YYYYMMDD-NNN-HHHHHHHHHHHH` 为主键；旧无哈希 ID 仍兼容。目录保存相对文件、日期、时间、记录序号、说话者、标签、内容哈希、最多 500 字符摘录和最后出现时间。

`run_sources` 以 `(run_id, source_id)` 为联合主键，表示一次报告运行使用过哪些记录。相同位置正文改变会得到新 ID，旧来源不会被覆写。

## 6. 旧画像结构兼容

人物画像不再是产品功能。为避免升级时破坏旧数据，数据库初始化仍识别紧邻旧版、恰好多出 `profile_entries` 与 `profile_feedback` 的结构：

1. 用 SQLite Backup API 创建 `.analysis.pre-profile-markdown.sqlite3`；
2. 将有效历史和反馈幂等导出到 `Profile.md`；
3. 从工作数据库移除旧画像表。

导出的 `Profile.md` 只作历史资料，新版报告不读取或更新。其他未知数据库结构仍直接拒绝，不猜测补表或删除。

## 7. 备份与恢复

- 必须备份 `Records/`、报告、分析数据库和含真实密钥的 `config.yaml`。
- 运行中的 SQLite 应使用 Backup API，或一致处理主库、WAL、SHM。
- 旧 `Profile.md` 与 `Information/` 可保留或另行归档，新版会忽略。
- 数据库错误不得触发对日记或报告的清理。
- 已生成报告阅读不依赖 SQLite；删除数据库会丢失审计和缓存，但不会截断报告正文。
