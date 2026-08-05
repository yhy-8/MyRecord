# SQLite 数据模型

`AnalysisReports/.analysis.sqlite3` 是可删除的运行缓存，不替代日记或报告。

## 1. analysis_runs

一行表示一次周报或月报运行，保存：

- 运行 ID、报告类型和周期；
- `manual`/`auto` 来源与触发方式；
- 实际模型、输入快照哈希和状态；
- 成功报告路径或失败原因；
- 创建与完成时间。

同周期重跑创建新行，固定 Markdown 文件可以被新成功运行覆盖。

## 2. agent_artifacts

一行表示一次 Agent 或中控搜索阶段产物，保存：

- 所属运行、阶段名和修订序号；
- 完成/失败状态与错误；
- 模型最小 JSON、中控组装结果或搜索证据；
- 调用耗时、Token、结束原因和搜索遥测。

唯一约束为 `(run_id, agent, revision)`。缓存只复用相同输入、模型和有效配置下已验证的阶段；复用阶段不计入本次 Token 用量。

## 3. 事务与重建

每个存储操作使用独立连接，启用外键、WAL、10 秒 busy timeout 和短 `BEGIN IMMEDIATE` 事务。事务不跨越模型或网络请求。

程序不迁移旧数据库。确认没有报告运行后，可以删除：

```text
AnalysisReports/.analysis.sqlite3
AnalysisReports/.analysis.sqlite3-wal
AnalysisReports/.analysis.sqlite3-shm
```

删除只清空审计和缓存，不影响日记、已有报告、自动任务状态或配置。
