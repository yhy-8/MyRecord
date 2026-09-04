# MyRecord

MyRecord 是一个**本地优先、多设备云端同步**的个人记录与周期回顾工具。你像写日记一样自由记录，
不需要预先分类，也不会把记录过程变成与 AI 的对话。

项目为**服务端中枢 + 客户端薄端**两段架构：

- **服务端（`server/`）**：数据中枢 —— 设备鉴权、条目合并/对账/扇出、垃圾桶、TLS，以及全部云端 AI
  （日记总结、自然周报、自然月报）。模型密钥只存服务端。
- **客户端（`client/`）**：薄记录端 —— 只负责写当天日记、实时同步、查看与常用命令；不保存模型密钥、
  不做本地 AI。

两端严格分离、各自独立部署（只共享极少的原子写入与日记格式，各自内置一份，互不导入）；以启动命令
决定运行角色（`python -m server.main run` / `python -m client`）。

## 目录结构

| 目录 | 内容 |
|---|---|
| `server/` | 服务端中枢与云端 AI（详见 [`server/README.md`](./server/README.md)） |
| `client/` | 客户端薄端（详见 [`client/README.md`](./client/README.md)） |
| `Docs/` | 设计基线与部署等权威说明 |
| `tests/` | 仓库级测试 |

## 快速开始

服务端（详见 [`server/README.md`](./server/README.md)）：

```bash
pip install -r server/requirements.txt
cp server/config.example.yaml server/config.yaml   # 填入模型 api_key
python -m server.main cert --ip 服务端地址
python -m server.main run
```

客户端（详见 [`client/README.md`](./client/README.md)）：

```bash
pip install -r client/requirements.txt
cp client/config.example.yaml client/config.yaml   # 填 server_url（verify 默认空=不校验；可选设 server.crt 切严格）
python -m client
```

## 数据与安全（要点）

- **原始日记是唯一事实源**；总结、报告、自动任务状态都是可重建的派生数据。
- 删改为垃圾桶语义（tombstone），不做硬删除。
- **加密通信强制**：服务端在 TLS 下运行（自签证书直连）；连接与修改日记都需服务端签发的连接凭证。
- **模型密钥只在服务端** `config.yaml`（已 gitignore），不入数据空间、不入日志。

安全、备份与设计规则详见 [`Docs/设计基线.md`](./Docs/设计基线.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [`Docs/设计基线.md`](./Docs/设计基线.md) | **唯一权威设计基线**：架构、同步、鉴权/TLS、存储、日志、云端 AI、自动化、备份 |
| [`Docs/代码结构.md`](./Docs/代码结构.md) | 代码模块职责与数据流 |
| [`Docs/部署指南.md`](./Docs/部署指南.md) | 简明部署步骤（服务端 + 凭证 + TLS + 客户端 + 备份） |
| [`server/README.md`](./server/README.md) / [`client/README.md`](./client/README.md) | 两端各自模块职责与命令 |

## 测试

```bash
python -m compileall -q server client tests
python -m unittest discover -s tests
```
