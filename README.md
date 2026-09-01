# MyRecord

MyRecord 是一个**本地优先、多设备云端同步**的个人记录与周期回顾工具。你像写日记一样自由记录，
不需要预先分类，也不会把记录过程变成与 AI 的对话。

项目重构为**服务端中枢 + 客户端薄端**两端架构（原单机 `MyRecord/` 包已移除）：

- **服务端（`server/`）**：数据中枢、设备鉴权、条目合并/对账/扇出、垃圾桶，以及全部云端 AI
  （日记总结、自然周报、自然月报）。模型密钥只存在服务端。
- **客户端（`client/`）**：薄记录端。只负责写当天日记、实时同步、查看与常用命令；**不保存模型密钥、
  不做本地 AI**。

## 目录结构

```text
server/                  服务端中枢 + 云端 AI（独立工程，见 server/README.md）
  main.py                服务端 CLI（run / token / import / render）
  config.yaml            服务端配置（监听、模型、重试、自动任务）
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
  hub/                   同步协议、存储、鉴权、日记格式(render)
  ai/                    云端 AI：agents / analysis(context,orchestrator,automation)

client/                  客户端薄端（独立工程，见 client/README.md）
  __main__.py            客户端入口（python -m client）
  config.yaml            客户端配置（服务器地址、数据目录、轮询间隔）
  config.py              客户端配置读取
  identity.py            设备身份：credentials.json（凭据）、seq.json（单调序号，entry_id 生成）
  outbox.json            离线待推送队列
  journal.py             本地日记渲染与写入
  sync.py                同步：写后即时 push / 离线队列 / full_sync / 长轮询扇出 / 报告同步
  cli.py                交互界面：命令路由/查看/清屏/日期解析、启动 full_sync、长连接后台线程

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

或使用 systemd（`server/deploy/myrecord-server.service`，启动后按注释修改解释器路径与
`WorkingDirectory` 为独立 server 工程绝对路径）。

常用服务端子命令：

```text
python -m server.main run                    启动同步+AI 服务
python -m server.main token create                 签发/重签唯一链接凭证（覆盖旧 token）
python -m server.main token list                   查看当前唯一凭证状态
python -m server.main token rotate                 重新签发（覆盖旧 token）
python -m server.main token revoke                 停用凭证（所有端失去同步）
python -m server.main import --records 路径  导入旧版 Records
python -m server.main render                 重渲染当天 Records
```

### 2. 客户端

安装依赖（在独立 client 工程内）：`pip install -r client/requirements.txt`。

将服务端签发的唯一链接凭证 `token` 写入 `client/credentials.json`（格式见样板
`client/credentials.example.json`；设备名默认用本机名，如 `MK8`、`vivo y78`），然后：

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
/retry        直接重试全部失败的服务端自动任务（无顺序依赖）
/model        永久切换服务端 AI 模型
普通输入       立即写入当天记录并即时同步到云端（长连接保持中）
```

日期可用 `today`、`昨天`、`-1`、`MM-DD`、`YYYY-MM-DD` 等写法。

## 数据与安全

- **原始日记是唯一事实源**；总结、报告、自动任务状态都是可重建的派生数据。
- 记录写入永不因同步失败回滚；删改为**垃圾桶语义**（tombstone），不做硬删除，防“已删条目复活”。
- **加密通信是强制的**：服务端 `run` 未配置 TLS 会拒绝启动（禁止明文）；`python -m server.main cert`
  用**服务端签发自己的自签公钥/证书**，客户端 `config.yaml` 的 `server_url=https` 且 `verify` 指向该
  证书即可校验收信（自签直连，无需反向代理）。
- **连接与修改日记都需服务端签发的连接凭证**（`token create` 生成的超长随机令牌）：无凭证只能本地
  记录、无法把修改传上服务端或拉取/删除云端数据。凭证是**单一共享 token**，服务端只存 scrypt 哈希，
  不绑定设备；设备由各端自报本机名区分，每条记录带上设备名。无凭证/离线时仍可本地记录，上线后按
  内容一致 id 自动合并。
- **服务端记录详细日志**（`server/data/Log/MyRecord.log`，滚动文件）：客户端连接/鉴权、对日志的
  推送与在线删除、AI 报告生成成败与每步 Agent 调用、自动任务重试等；**不记录**日记原文、模型密钥、
  token 明文。
- 模型密钥只在服务端；不入数据空间、不入日志。
- 日记文件的条目/删除标记统一采用 `<!-- myrecord-* -->` 格式。旧数据（`agentrecord-*`，含远古
  无标记的裸 `**HH:MM:** 正文`）无需迁移，解析器原生向后兼容：无 entry_id 的记录自动生成 legacy id，
  AI 只读取正文文本，任何旧标记都不会混入正文。

## 文档结构（总体 / 客户端 / 服务端）

文档按三层组织，读完即可大致了解项目代码做什么：

| 层级 | 文档 | 内容 |
|---|---|---|
| **设计基线（唯一权威）** | [`Docs/设计基线.md`](./Docs/设计基线.md) | 整合全部设计：架构、同步、鉴权/TLS、存储、日志、云端 AI 分析（单次 Report Agent + [N] 引用；来源表与引用校验当前不实现）、自动化 |
| 总体工作配合说明 | 本文件（README.md） | 两端如何协作、仓库代码总览、启动/运行/测试 |
| 客户端说明 | [`client/README.md`](./client/README.md) | client/ 每个文件/模块的职责与命令 |
| 服务端说明 | [`server/README.md`](./server/README.md) | server/ 每个文件/模块的职责、协议与部署 |
| 历史/深度参考 | [`Docs/代码结构.md`](./Docs/代码结构.md) | 结构与安全细节；与基线冲突处以基线为准 |

## 自动任务

服务端后台线程每 15 分钟检测缺失、独立执行到期任务（日总结 / 周报 / 月报互不依赖）；失败后
30 分钟自动重试，每种任务按各自重试次数上限（默认 2）停止。查看状态：客户端 `/status`；
直接重试全部失败项：`/retry`。

## 文档

- [`Docs/设计基线.md`](./Docs/设计基线.md) — **唯一权威设计基线**：整合所有设计（含单次 Report Agent、[N] 引用、无 AI 审核的报告生成；来源表与引用校验当前不实现）
- [`Docs/代码结构.md`](./Docs/代码结构.md) — 历史结构说明（代码模块职责、数据流、entry_id、同步协议）
- [`Docs/部署指南.md`](./Docs/部署指南.md) — 简明部署步骤（服务端 + 凭证 + TLS + 客户端 + 备份）

> 根目录已不再提供 `main.py` / `config.yaml`（旧单机残留已删除）。
> 入口严格区分：客户端用 `python -m client`，服务端用 `python -m server.main run`。