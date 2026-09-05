# MyRecord 服务端（中枢 + 云端 AI）

数据中枢与云端 AI：设备鉴权、条目合并/对账/扇出、垃圾桶、自动任务（日总结、自然周报、
自然月报）。模型密钥只存这里。日志不保存私密原文。本项目**服务端独立部署**：客户端与服务端
严格分离，各自内置一份原子写入 `atomic_write.py` 与日记格式 `render.py`，互不导入、不共用 `common/`；
以 `python -m server.main run` 启动即为服务端。

## 安装与启动

在**独立 server 工程根**（即包含 `server/` 目录的那一层，`python -m server.main` 需从这一层运行）执行：

```bash
pip install -r server/requirements.txt
cp server/config.example.yaml server/config.yaml   # 生成运行配置，再在其中填入模型 api_key
python -m server.main run
```

> 入口统一为 `python -m server.main run`（`server/main.py`），需在包含 `server/` 工程根运行。
> `server/config.example.yaml` 是**配置模板**（空白 api_key，已提交）；运行时读取的是 `server/config.yaml`
> （含 api_key，已 gitignore，不入版本库），请按上面两步用模板生成并**填入你的模型密钥**。
> 启动后自带**后台调度线程**：每 15 分钟检测缺失，并独立执行到期任务（日总结 / 周报 / 月报
> 互不依赖、无顺序要求）。该线程属服务端调度，与客户端同步无关。

## 子命令

```text
python -m server.main run            启动同步+AI 服务
python -m server.main token create                 签发/重签唯一链接凭证（覆盖旧 token，需二次确认）
python -m server.main token list                   查看当前唯一凭证状态（含生成时间）
python -m server.main import --records 路径    导入既有 Records
python -m server.main render          重渲染当天 Records
python -m server.main report --kind weekly|monthly --date YYYY-MM-DD   手动生成周/月报（同流程，直接覆盖）
python -m server.main cert             生成自签证书（服务端直连 TLS，`--ip` 可指定 SAN）
python -m server.main deploy            一键安装并启动 systemd 服务（需 root）
```

## 角色：与客户端的协作

- **同步**：暴露 `push / pull / longpoll / delete / status / reports / health` HTTP 接口
  （见 `hub/server.py`）。客户端以**长连接（长轮询）**挂起在 `longpoll`，服务端有新条目/
  报告时立即返回（扇出）；客户端**后台持续同步**：连接成功即完整对账，之后保持长连接接收
  扇出，断线自动重连补齐，不密集轮询、无需手动同步。
- **云端 AI**：自动任务写入 `<summary>` 与周/月报告（`AnalysisReports/`），客户端通过
  `/api/reports` 拉取本地副本。
- **数据空间**：`server/data/` 是权威事实源，客户端本地只是对账副本。

## 代码总览（每个文件做什么）

### 入口与配置

| 文件 | 职责 |
|---|---|
| `main.py` | 服务端 CLI：`run`（起 hub + AI 自动任务线程）、`token`（连接凭证管理）、`cert`（自签 TLS 证书）、`deploy`（一键 systemd）、`import`（导入既有 Records）、`render`（重渲染 Records） |
| `config.py` / `config.yaml` | 读取服务端配置（监听、模型、重试、自动任务、数据目录） |

### 同步中枢（hub/）

| 文件 | 职责 |
|---|---|
| `hub/server.py` | HTTP 同步服务（stdlib ThreadingHTTPServer）：`/api/sync/push`、`/api/sync/pull`、`/api/sync/longpoll`、`/api/entries/delete`、`/api/status`、`/api/reports`、`/api/admin/*`；Bearer + device_id 鉴权 |
| `hub/store.py` | 权威条目存储：append-only 合并（按 entry_id 去重）、tombstone、垃圾桶、设备令牌、全局 `version` 同步游标、`wait_for_change`（长轮询等待）、拉取 `pull(version)` |
| `hub/auth.py` | 链接凭证令牌哈希（scrypt，加盐、常量时间），只存哈希，不落明文 |
| `hub/atomic_write.py` | 原子文件写入（服务端自带小工具，与客户端各自独立） |
| `hub/render.py` | 日记文件格式（服务端权威；客户端独立镜像同款格式，由测试锁齐）：渲染 entry 标记、tombstone 占位、`<summary>` 区域，并反向解析文件为条目列表（识别多种条目标记格式） |

### 云端 AI（ai/）

| 文件 | 职责 |
|---|---|
| `ai/settings.py` | 运行配置、目录与模型选择（`current_model` / `models`） |
| `ai/ai_client.py` | OpenAI 兼容模型请求、HTTP/thinking/JSON 输出、Token 遥测、错误分类 |
| `ai/journal.py` | 原始日记读写与 `<summary>` 区域更新（AI 只能通过这里写日记） |
| `ai/agents/` | 单模块：AgentSpec、提示词（含 JSON 结构与示例）；单次 Report Agent |
| `ai/analysis/context.py` | 周期范围、按天分块并标注行号的记录流、报告路径 |
| `ai/analysis/orchestrator.py` | 单次 Report Agent、纯 JSON（summary+references）解析与引用校验、Token 累计、审计头部、文末来源表、原子交付报告（权威说明见 `../Docs/设计基线.md` §8） |
| `ai/analysis/automation.py` | 简调度器：每 15 分钟缺失检测、失败后 30 分钟重试、按失败次数上限停止、每任务状态持久化 |
| `ai/file_lock.py`、`ai/logging_config.py` | 跨进程互斥、有界诊断日志 |

## 配置

服务器配置以 `server/config.example.yaml` 为**模板**（提交到版本库，`api_key` 留空）；运行时读取
`server/config.yaml`（已 gitignore，不入版本库），由用户复制模板后填写：

```bash
cp server/config.example.yaml server/config.yaml
# 编辑 config.yaml：在 models 的 api_key 处填入你的模型密钥
```

`config.yaml`：监听地址/端口、`models`、`current_model`、`retry`（失败/重试策略）、
`automation`（开关）、数据目录（`data_dir`、`diary_dir`、`analysis_dir`、`log_dir`，相对路径以
`server/` 为基准）。周报生成不做联网检索，无搜索配置。模型密钥只存在于 `config.yaml`，不入数据空间、不入日志。

## 部署

一键安装并启动为 systemd 服务（需 root，自动带出当前解释器与工程根，证书缺失时自动生成）：

```bash
sudo python -m server.main deploy
```

`deploy` 写入服务端单元（`myrecord-server.service`），并安装与启用每周备份定时器
（`myrecord-backup.service` + `myrecord-backup.timer`，`systemctl enable --now`）。

`server/deploy/`：

- `myrecord-server.service` — 服务端单元（`deploy` 自动生成/写入；此文件为等价参照）。
- `myrecord-backup.service` / `myrecord-backup.timer` — 每周自动备份单元与定时器（`deploy` 自动生成/写入；此为等价参照）。
- `backup.sh` — 备份 `data` 空间为 tar（保留最近 N 份）。

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
- 日记文件统一使用 `<!-- myrecord-* -->` 条目/删除标记。解析器识别多种标记格式（`myrecord-*`、
  `agentrecord-*`、无标记裸行）；无 entry_id 的记录自动生成确定性 id，标记注释不会混入正文，AI 只读文本。