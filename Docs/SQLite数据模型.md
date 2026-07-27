# SQLite 数据模型

分析数据库默认位于 `AnalysisReports/.analysis.sqlite3`。它只保存可重建的运行审计、Agent 产物/阶段缓存和记录来源目录，不替代 Markdown 日记，也不保存人物画像。人物画像及用户反馈的权威存储是相邻的 `AnalysisReports/Profile.md`。

## 1. 设计目标

SQLite 当前只承担：

- 审计每日画像、每日信息简报、周报和月报运行；
- 保存 Agent 成功或失败产物、请求遥测及安全阶段缓存；
- 保存搜索查询、结果和报告实际使用的证据；
- 将运行中的 `R-*` 映射到日记位置；
- 为允许零候选的每日画像提供 completed 完成证明。

外部研究和人物画像都不进入 SQLite 业务表。前者只属于当期 Markdown 报告，后者需要长期备份和直接读取，因此保存在 `Profile.md`。

## 2. 连接与事务

每个存储操作创建独立连接，并启用：

- `PRAGMA foreign_keys = ON`
- `PRAGMA busy_timeout = 10000`
- `PRAGMA journal_mode = WAL`
- 写事务使用 `BEGIN IMMEDIATE`

事务只覆盖 SQL，不跨越模型或网络请求。普通日记记录不访问 SQLite，因此数据库锁定或损坏不能阻止用户写日记。

## 3. analysis_runs

一行表示一次每日画像、每日信息简报、周报或月报运行。

| 字段 | 含义 |
|---|---|
| `id` | 32 位 UUID 十六进制运行 ID |
| `kind` | `daily_profile`、`daily_information`、`weekly` 或 `monthly` |
| `period_start/end` | 分析闭区间日期；每日任务两者相同 |
| `origin` | `manual` 或 `auto` |
| `trigger` | `manual`、`scheduled` 或 `retry` |
| `model_name` | 本次模型显示名 |
| `status` | `running`、`completed`、`failed` |
| `input_hash` | 完整输入快照 SHA-256 |
| `report_path` | 成功交付文件路径；每日画像为 `NULL` |
| `error` | 失败原因 |
| `created_at/completed_at` | 本地时间戳 |

手动触发只允许 `origin=manual, trigger=manual`。每日画像和信息简报只允许 `origin=auto`。系统计划任务首次执行使用 `trigger=scheduled`；自动或 `/retry` 重试使用 `trigger=retry`。

每次重跑都插入新行。Markdown 固定路径可以被覆盖，运行 ID 仍保留每次尝试的审计身份。

## 4. agent_artifacts

该表保存一次运行内各 Agent 与中控阶段的 JSON 产物。

| 字段 | 含义 |
|---|---|
| `id` | 产物 ID |
| `run_id` | 所属运行 |
| `agent` | Agent 或审查/检索阶段名 |
| `revision` | 同运行、同 Agent 从 1 递增的版本号 |
| `status` | 通常为 `completed` 或 `failed` |
| `payload_json` | 结构化载荷 |
| `error` | 解析、校验、审查或调用错误 |
| `created_at` | 创建时间 |

唯一约束为 `(run_id, agent, revision)`。模型回答格式错误、结构校验失败、Reviewer 输出不完整或审查未通过时也保存失败产物。`payload_json._telemetry` 保存请求、token、缓存 token 和搜索遥测，`payload_json._cache` 标记安全阶段复用。

`daily_information_search` 和 `research_search` 是中控阶段而不是模型 Agent：前者保存每日固定查询、`I-*` 证据和部分搜索错误；后者保存报告选题、`W-*` 证据及搜索遥测。只有输入、模型、搜索配置和流水线版本一致，且证据仍通过确定性校验时，失败运行重试才能复用。

## 5. source_catalog 与 run_sources

`source_catalog` 以稳定 `R-YYYYMMDD-NNN-HHHHHHHHHHHH` 为主键；旧数据的无哈希 `R-YYYYMMDD-NNN` 仍兼容。目录保存：

- `relative_path`
- `source_date`、`source_time`
- `record_index`
- `speaker`、`tag`
- `content_hash`
- 最多 500 字符 `excerpt`
- `last_seen_at`

末尾 12 位哈希覆盖日期、时间、标签、说话者和完整正文。同一位置的日记内容发生变化时产生新的来源 ID，旧目录项不会被改写。若同一 ID 指向任何不同记录，整笔事务失败；相同记录只刷新 `last_seen_at`。

`run_sources` 以 `(run_id, source_id)` 为联合主键，表示一次运行使用过哪些记录。外键保证运行不能指向不存在的来源目录项。

## 6. Profile.md

`Profile.md` 不属于 SQLite schema，但和分析数据库放在同一目录。它使用：

- YAML 前置区：`format: agentrecord-profile-v1`、更新时间、完整 `entries` 版本链和 `feedback` 事件；
- 可读正文：按类别展示当前画像，并列出历史版本和反馈；
- `.profile.lock`：协调报告提交和 `/f` 用户反馈；
- 唯一临时文件 + 原子替换：避免半写文件。

画像类别固定为 `viewpoint`、`principle`、`ideal`、`behavior_pattern` 和 `interest`。每个条目保存持久 ID、产生它的运行 ID 与周期、标题、陈述、置信度、`R-*` 来源、观察区间、创建者、替代目标和时间。用户反馈保存 `accept|reject|correct`、原条目、可选替代条目和时间。

有效画像按查询截止日重建：

- `last_observed` 不得晚于截止日；
- Retrospective 条目的 `period_end` 不得晚于截止日；
- 用户条目和反馈按 `created_at` 日期生效；
- 生效的新版本替代旧版本，生效的 reject 隐藏原版本。

因此以后发生的替代、修正或否决不会泄漏到过去报告。画像候选只有通过 Reviewer 且整个报告正文可交付时才提交；报告文件、画像文件或运行完成任一步失败，中控都会恢复写入前快照。

## 7. 上一版画像迁移

数据库不记录 `user_version` 或业务 schema 版本。启动时直接核对表集合和字段集合：

- 空数据库按当前四表结构创建；
- 当前结构完全一致时直接使用；
- 恰好是上一版结构（当前四表加 `profile_entries`、`profile_feedback`，且字段完全一致）时执行一次窄迁移；
- 其他未知结构直接报错，不猜测、不补表、不删除。

窄迁移顺序：

1. 用 SQLite Backup API 创建 `.analysis.pre-profile-markdown.sqlite3`；
2. 只导出 completed 运行中已接受/被替代、用户创建或带用户否决事件的有效历史，不导出 Reviewer 已拒绝候选和失败运行候选；
3. 幂等合并到 `Profile.md`；同 ID 内容冲突时停止；
4. 成功后从工作数据库删除旧画像表。

Markdown 已写而删表失败时，重试会再次进行无冲突幂等合并；数据库备份已存在时不会覆盖原备份。

## 8. 备份与恢复

- `Records/`、`Profile.md` 和含真实密钥的 `config.yaml` 是更新运行目录时必须明确保留的文件。
- `Profile.md` 可作为普通文本直接备份；恢复后程序会重新校验前置区、ID、类别、引用关系和版本链。
- 运行中的 SQLite 应使用 Backup API，或一致处理主库、WAL、SHM，不能只复制主文件。
- `.analysis.pre-profile-markdown.sqlite3` 应保留到迁移结果人工核对完成。
- 数据库错误不得触发对 `Records/` 或 `Profile.md` 的清理。
- 已生成报告包含完整正文、外部链接和 `R-*` 来源索引，阅读不依赖 SQLite；删除数据库会丢失运行审计和缓存，但不会截断报告或人物画像文件。
