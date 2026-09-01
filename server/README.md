# MyRecord 服务端（中枢 + 云端 AI）

数据中枢与云端 AI：设备鉴权、条目合并/对账/扇出、垃圾桶、自动任务（日总结、自然周报、
自然月报）。模型密钥只存这里。日志不保存私密原文。本目录是**独立可发布的服务器工程**，
不依赖 `client/`，可直接拷贝/部署运行。

## 安装与启动

```bash
pip install -r requirements.txt
python -m server.main run
```

> 入口统一为 `python -m server.main run`（`server/main.py`）。
> 启动后自带**后台调度线程**：每 15 分钟检测缺失，并独立执行到期任务（日总结 / 周报 / 月报
> 互不依赖、无顺序要求）。该线程属服务端调度，与客户端同步无关。

## 子命令

```text
python -m server.main run            启动同步+AI 服务
python -m server.main token create                 签发/重签唯一链接凭证（覆盖旧 token）
python -m server.main token list                   查看当前唯一凭证状态
python -m server.main token rotate                 重新签发（覆盖旧 token）
python -m server.main token revoke                 停用凭证（所有端失去同步）
python -m server.main import --records 路径    导入旧版 Records
python -m server.main render          重渲染当天 Records
python -m server.main cert             生成自签证书（服务端直连 TLS，`--ip` 可指定 SAN）
```

## 角色：与客户端的协作

- **同步**：暴露 `push / pull / longpoll / delete / status / reports / health` HTTP 接口
  （见 `hub/server.py`）。客户端以**长连接（长轮询）**挂起在 `longpoll`，服务端有新条目/
  报告时立即返回（扇出）；客户端只在启动/手动 `/sync` 做完整对账，不会密集轮询。
- **云端 AI**：自动任务写入 `<summary>` 与周/月报告（`AnalysisReports/`），客户端通过
  `/api/reports` 拉取本地副本。
- **数据空间**：`server/data/` 是权威事实源，客户端本地只是对账副本。

## 代码总览（每个文件做什么）

### 入口与配置

| 文件 | 职责 |
|---|---|
| `main.py` | 服务端 CLI：`run`（起 hub + AI 自动任务线程）、`token`（设备令牌管理）、`import`（旧数据导入）、`render`（重渲染 Records） |
| `config.py` / `config.yaml` | 读取服务端配置（监听、模型、重试、自动任务、数据目录） |

### 同步中枢（hub/）

| 文件 | 职责 |
|---|---|
| `hub/server.py` | HTTP 同步服务（stdlib ThreadingHTTPServer）：`/api/sync/push`、`/api/sync/pull`、`/api/sync/longpoll`、`/api/entries/delete`、`/api/status`、`/api/reports`、`/api/admin/*`；Bearer + device_id 鉴权 |
| `hub/store.py` | 权威条目存储：append-only 合并（按 entry_id 去重）、tombstone、垃圾桶、设备令牌、全局 `version` 同步游标、`wait_for_change`（长轮询等待）、拉取 `pull(version)` |
| `hub/auth.py` | 链接凭证令牌哈希（scrypt，加盐、常量时间），只存哈希，不落明文 |
| `hub/render.py` | 日记文件格式（服务端权威；客户端独立镜像同款格式，由测试锁齐）：渲染 entry 标记、tombstone 占位、`<summary>` 区域，并反向解析文件为条目列表（兼容新旧 entry 标记） |

### 云端 AI（ai/）

| 文件 | 职责 |
|---|---|
| `ai/settings.py` | 运行配置、目录与模型选择（`current_model` / `models`） |
| `ai/ai_client.py` | OpenAI 兼容模型请求、HTTP/thinking/JSON 输出、Token 遥测、错误分类 |
| `ai/journal.py` | 原始日记读写与 `<summary>` 区域更新（AI 只能通过这里写日记） |
| `ai/agents/` | 单模块：AgentSpec、纯文本协议；单次 Report Agent 生成完整报告正文 |
| `ai/analysis/context.py` | 周期范围、按日期分隔并标注行号的记录流、全局引用编号、报告路径 |
| `ai/analysis/orchestrator.py` | 单次 Report Agent、[N] 全局引用编号、Token 累计、审计头部、原子交付报告；来源表与引用校验当前不实现（权威说明见 `../Docs/设计基线.md` §8） |
| `ai/analysis/automation.py` | 简调度器：每 15 分钟缺失检测、失败后 30 分钟重试、按失败次数上限停止、每任务状态持久化 |
| `ai/file_lock.py`、`ai/logging_config.py` | 跨进程互斥、有界诊断日志 |

## 配置

`server/config.yaml`：监听地址/端口、`models`、`current_model`、`retry`（失败/重试策略）、
`automation`（开关）、数据目录（`data_dir`、`diary_dir`、`analysis_dir`、`log_dir`，相对路径以
`server/` 为基准）。周报不再联网检索，无搜索配置。

## 部署

`server/deploy/`：

- `myrecord-server.service` — systemd 单元（安装/启停说明见文件头注释）。
- `backup.sh` — 备份 `data` 空间为 tar（保留最近 N 份）。
- `restore.sh` — 从备份恢复 `data` 空间。

数据空间（运行时生成）：`server/data/` — `state.json`（权威条目/设备/垃圾桶）、
`Records/`（渲染每日日记）、`Trash/`（被删正文）、`AnalysisReports/`（报告与自动任务状态）、
`Log/`（服务端日志）。

## 数据与安全

- 原始日记是唯一事实源；报告、总结、自动任务状态都是可重建派生数据。
- 删改为垃圾桶语义（tombstone），不做硬删除。
- 链接凭证是**单一共享 token**（服务端只存 scrypt 哈希），凭证不绑定设备；设备由各端自报本机名区分。
  **连接与修改日志都必须携带该凭证**（无凭证客户端只能本地记录，无法同步/删除/查看云端数据）。
- **加密传输是强制的**：`run` 未配置 TLS 会拒绝启动（禁止明文）。`python -m server.main cert` 用
  **服务端自签证书（自签公钥）**，客户端 `verify` 校验收信（自签直连，无需反向代理）。
- **服务端记录详细日志**到 `data/Log/MyRecord.log`：客户端连接/鉴权、对日志的推送与在线删除、AI 报告
  生成成败与每步 Agent 调用、自动任务重试；不记录日记正文、模型密钥、token 明文。
- 模型密钥只在服务端；不入数据空间、不入日志。
- 日记文件统一使用 `<!-- myrecord-* -->` 条目/删除标记。旧数据（`agentrecord-*` 与远古裸记录）
  原生向后兼容：解析时无 entry_id 自动生成 legacy id，标记注释不会混入正文，AI 只读文本。