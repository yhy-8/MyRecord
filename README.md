# AgentRecord

AgentRecord 是一个**本地优先、多设备云端同步**的个人记录与周期回顾工具。你像写日记一样自由记录，
不需要预先分类，也不会把记录过程变成与 AI 的对话。

项目重构为**服务端中枢 + 客户端薄端**两端架构（原单机 `AgentRecord/` 包已移除）：

- **服务端（`server/`）**：数据中枢、设备鉴权、条目合并/对账/扇出、垃圾桶，以及全部云端 AI
  （日记总结、自然周报、自然月报）。模型密钥只存在服务端。
- **客户端（`client/`）**：薄记录端。只负责写当天日记、实时同步、查看与常用命令；**不保存模型密钥、
  不做本地 AI**。

架构决策与历史背景见 [`Docs/重构计划与功能说明.md`](./Docs/重构计划与功能说明.md)。

## 目录结构

```text
server/                  服务端中枢 + 云端 AI（独立工程，见 server/README.md）
  main.py                服务端 CLI（run / token / import / render）
  config.yaml            服务端配置（监听、模型、搜索、重试、自动任务）
  config.py              服务端配置读取
  requirements.txt      服务端依赖（pyyaml、requests）
  README.md              服务端独立工程说明
  deploy/                systemd 单元与备份/恢复脚本
  data/                  数据空间（运行时生成）
    state.json           权威条目/设备/垃圾桶状态
    Records/             渲染出的每日日记（服务端视角）
    Trash/               被删正文（垃圾桶）
    AnalysisReports/     报告目录
      .automation-state.json   自动任务状态
    Log/                 服务端日志
  hub/                   同步协议、存储、鉴权、渲染
  ai/                    云端 AI：agents / analysis(context,orchestrator,automation)

client/                  客户端薄端（独立工程，见 client/README.md）
  main.py / __main__.py  客户端入口（python -m client）
  config.yaml            客户端配置（服务器地址、数据目录、轮询间隔）
  config.py              客户端配置读取
  credentials.json       本地凭据（服务端签发，首登后写入，不入中枢）
  seq.json               本设备单调序号（entry_id 生成）
  outbox.json            离线待推送队列
  journal.py / render.py 本地日记写入与渲染
  sync.py                同步：写后即时 push / 离线队列 / full_sync / 长轮询扇出 / 报告同步
  cli/                   交互界面与命令

Docs/                    设计与深度说明文档（仓库级）
tests/                   测试（仓库级：server hub / client sync / ai analysis …）
```

> `client/` 与 `server/` 各自是**可独立拷贝/发布的完整工程**（各自含 README、requirements、
> config，且互不 import）。仓库根目录只保留联合开发/测试/文档的脚手架。

## 启动

### 1. 服务端（Linux，建议 root / systemd）

安装依赖（在独立 server 工程内）：

```bash
pip install -r server/requirements.txt
```

运行（前台）：

```bash
python -m server.main run
```

或使用 systemd（`server/deploy/agentrecord-server.service`，启动后按注释修改解释器路径与
`WorkingDirectory` 为独立 server 工程绝对路径）。

常用服务端子命令：

```text
python -m server.main run                    启动同步+AI 服务
python -m server.main token create --device 名称   签发新设备令牌（首登用）
python -m server.main token list             列出活动设备
python -m server.main token rotate --device 名称   轮换令牌
python -m server.main token revoke --device 名称   停用设备
python -m server.main import --records 路径  导入旧版 Records
python -m server.main render                 重渲染当天 Records
```

### 2. 客户端

安装依赖（在独立 client 工程内）：`pip install -r client/requirements.txt`。

首次在客户端写入服务端签发的凭据（`device_id` 与 `token`）到 `client/credentials.json`，然后：

```bash
python -m client
```

客户端启动时自动链接云端并完整同步（拉取对账 + 冲刷离线队列 + 同步报告）；运行期间保持
一条长连接（长轮询）接收扇出，每条记录写入即即时 push，不密集轮询云端；手动同步用 `/sync`。

## 基本使用（客户端，统一 8 个命令）

```text
/v [日期]     查看本地日记（不提供报告查看；报告建议用专用 md 阅读软件打开）
/c            清屏
/h            帮助
/sync         立即手动完整同步一次（推送离线队列、拉取对账、同步报告）
/d            在线删除当天最新一条（需联网，服务端确认，正文入垃圾桶）
/status       查看服务端 AI 自动任务状态
/retry        按队列重试服务端失败的自动任务
/model        永久切换服务端 AI 模型
普通输入       立即写入当天记录并即时同步到云端（长连接保持中）
```

日期可用 `today`、`昨天`、`-1`、`MM-DD`、`YYYY-MM-DD` 等写法。

## 数据与安全

- **原始日记是唯一事实源**；总结、报告、自动任务状态都是可重建的派生数据。
- 记录写入永不因同步失败回滚；删改为**垃圾桶语义**（tombstone），不做硬删除，防“已删条目复活”。
- 设备鉴权用长令牌；服务端只存令牌哈希（scrypt）。传输应为 HTTPS（当前样例默认 http，上线请启用 TLS）。
- 模型密钥只在服务端；不入数据空间、不入日志。

## 文档结构（总体 / 客户端 / 服务端）

文档按三层组织，读完即可大致了解项目代码做什么：

| 层级 | 文档 | 内容 |
|---|---|---|
| 总体工作配合说明 | 本文件（README.md） | 两端如何协作、仓库代码总览、启动/运行/测试 |
| 客户端说明 | [`client/README.md`](./client/README.md) | client/ 每个文件/模块的职责与命令 |
| 服务端说明 | [`server/README.md`](./server/README.md) | server/ 每个文件/模块的职责、协议与部署 |
| 深度设计参考 | [`Docs/`](./Docs) | 架构原则、同步机制、AI 报告流程、重构/测试/审查 |

## 自动任务

服务端每分钟检查到期任务，自动执行依赖顺序：**昨日日记总结 → 上一完整自然周周报 → 上一完整自然月月报**。
查看状态：客户端 `/status`；重试失败项：`/retry`。

## 文档

- [`Docs/设计原则与系统架构.md`](./Docs/设计原则与系统架构.md) — 产品边界、核心原则、模块职责
- [`Docs/Agent与报告流程.md`](./Docs/Agent与报告流程.md) — 云端 AI 各阶段与检索流程
- [`Docs/运行机制与数据.md`](./Docs/运行机制与数据.md) — 配置、文件布局、自动任务、故障恢复
- [`Docs/部署指南.md`](./Docs/部署指南.md) — 端口开放、systemd 部署、签发设备凭证、备份与加密传输
- [`Docs/重构计划与功能说明.md`](./Docs/重构计划与功能说明.md) — 两端架构的已定稿功能契约

> 根目录已不再提供 `main.py` / `config.yaml`（旧单机残留已删除）。
> 入口严格区分：客户端用 `python -m client`，服务端用 `python -m server.main run`。