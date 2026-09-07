# MyRecord 客户端（薄端）

本地优先的记录客户端：写当天日记、同步到服务端中枢、查看与常用命令。
**不保存模型密钥、不做本地 AI**；鉴权凭据由服务端签发。本项目**客户端独立部署**：客户端与服务端
严格分离，各自内置一份原子写入 `atomic_write.py` 与日记格式 `render.py`，互不导入、不共用 `common/`；
以 `python -m client` 启动即为客户端，以 `python -m server.main run` 启动即为服务端。

## 交互入口

在**独立 client 工程根**（即包含 `client/` 目录的那一层，`python -m client` 需从这一层运行）执行：

```bash
python -m client
```

> 入口统一为 `python -m client`（`client/__main__.py` 直接调用 `cli.run_interactive`）。
> 打包命令见仓库根 `.github/workflows/build.yml`（PyInstaller 入口 `client/__main__.py`）。

## 同步模型（与云端的协作方式）

客户端**不密集轮询云端**，且**同步是全自动、无感知的**：

1. **启动即自动完整同步**：连接云端完整对账（从 `version=0` 重建/补齐本地**“今天”**镜像，
   覆盖本地文件丢失；按删除标记移除）、冲刷离线队列、同步报告；并对每个 `date < 今天` 的
   历史日记做**整文件校验**，不一致即以云端为准覆盖本地（历史日只读）。
2. **运行期间持续同步**：后台线程保持一条长连接（长轮询）挂起，服务端有更新（扇出）即
   返回并立即应用；写下的每条记录即触发 push（写后即时同步）。**服务端离线再上线后，
   后台线程会自动重新连接并完整对账**，无需手动同步。

客户端只在前台交互运行时保持后台同步线程；关闭程序即无任何后台任务。断网时本地照常记录并
进 `outbox.json` 离线队列，恢复后由后台线程自动冲刷补齐。

> **只写“今天”（UTC+8，固定写死在代码中，非配置项）。** 客户端把当天记录写入本地并在当天推送；
> 某条记录若未能在当天同步、拖到第二天，则**直接作废**（丢弃 `outbox.json` 中 `date < 今天` 的条目，
> 不再推送）；服务端亦拒绝历史日的推送作双保险。历史日记从不被客户端改写。

## 配置

客户端配置以 `client/config.example.yaml` 为**模板**（已提交）；运行时读取 `client/config.yaml`
（已 gitignore，不入版本库），由用户复制模板后填写：

```bash
cp client/config.example.yaml client/config.yaml
# 编辑 config.yaml：把 server_url 改成你的服务端中枢地址,按需设置 verify
```

`config.yaml`：服务器地址、本地数据目录（Records / AnalysisReports）。相对路径以 `client/`
为基准；默认 `../Records`、`../AnalysisReports` 指向 `client` 的**同级目录（项目根）**，
把记录/报告与代码包 `client/` 分开存放。本地数据目录不入服务端中枢。
`server_url` 默认 `https://localhost:8765`（服务端强制 TLS）；`verify` 留空时不校验证书，
设为服务端 `server.crt` 路径时严格校验收信。每台客户端启动不会打印 urllib3 的
`InsecureRequestWarning`，避免污染交互终端。

> 打包成 exe 运行时（见 `.github/workflows/build.yml`），把 `config.example.yaml` 模板拷到
> exe 同级目录作为 `config.yaml`（包内只保留 `config.example.yaml` 模板）；凭据 credentials.json
> 与本地数据目录同样以 **exe 同级目录** 为基准（用户直接在 exe 旁编辑 config.yaml）。

## 命令（统一 7 个）

```text
/v [日期]   查看某天本地日记（默认今天；日期：今天/昨天/-N/MM-DD/YYYY-MM-DD）
/c          清屏
/h          帮助
/d          在线删除当天最新一条（需联网，服务端确认，正文入垃圾桶）
/status     查看服务端 AI 自动任务状态
/retry      直接重试全部失败的服务端自动任务（无顺序依赖）
/model      永久切换服务端 AI 模型
```

`/status` 输出的含义（均为**今天** UTC+8 的视角，历史日以 `Records/*.md` 整文件为准）：

- `今日条目` / `今日已删`：**仅当天**（UTC+8）有效条目数 / 已删除（tombstone）条数。历史日条目
  **不计数**（封存进整文件后不再进入 `state.json`）。
- 自动任务逐任务状态（服务端严格以文件为准判定）：
  - `已完成`：产物文件已生成（昨日总结 / 周月报）。
  - `无内容（该周期无记录）`：该周期没有日记，无事可做——注意：**不代表**生成了内容。
  - `待生成` / `正在生成`：有内容但尚未生成 / 正在生成。
  - `失败（待重试）`：生成失败，附失败原因与下次重试时间，会自动重试达上限。
  - `失败（已停止自动重试，需手动重试）`：达重试上限，需 `/retry` 手动再试。
  - `未配置（无AI）`：模型未配置 / 缺 `api_key`，无法生成；补配置后重启服务端即可。
- 若你确认某天有记录却显示 `无内容` 且 `/v` 里没内容，两种常见原因：
  1. 该内容**尚未同步到服务端**——服务端据此判「无内容」；确认后等待下轮检测即可（检测每 15 分钟一次）。
  2. **服务端其实已生成，但本地是旧副本**——历史日的 `Records/*.md` 只在启动/重连的完整同步时被刷新；
     若总结是在客户端启动**之后**生成的，本地的 `/v` 仍显示旧的占位符，重启客户端或重新触发完整同步即可看到。

## 代码总览（每个文件做什么）

| 文件 | 职责 |
|---|---|
| `__main__.py` | 程序入口：`python -m client` → `run_interactive()` |
| `config.py` / `config.example.yaml` | 读取本地配置（服务器地址、数据目录）。`config.example.yaml` 是提交的模板；运行时读取 `config.yaml`（已 gitignore），由用户复制模板并填服务器地址 |
| `identity.py` | 链接凭证与设备身份：读写 `credentials.json`（单一共享 token）；`device_name()` 直接用本机名（电脑名/手机名，不允许自定义）；`make_entry_id(ts)` 以毫秒时间戳为 id（时间戳即标识，不做内容哈希） |
| `atomic_write.py` | 原子文件写入（客户端自带小工具，与服务端各自独立） |
| `render.py` | 日记文件格式本地渲染（标记/entry/tombstone/day_header；客户端自带，与服务端 hub/render.py 同款互不引用） |
| `journal.py` | 本地日记渲染与写入：**只写“今天”（UTC+8）** `Records/YYYY-MM-DD.md` 原子追加；对账**只对今天**做 `(ts, entry_id)` 时间有序合并；昨天及以前**整文件重放**（云端为准、覆盖本地）；过期未同步、`date < 今天` 的条目直接作废；tombstone 移除 |
| `file_lock.py` | 跨进程互斥（`.journal.lock` 等），保证原子写 |
| `sync.py` | 与中枢的同步客户端：`push_new`（写后即 push，**仅今天**）、`send_pending`（冲刷离线队列，过期即丢弃）、`pull`（拉取今天对账）、`longpoll`（长连接扇出）、`full_sync`（启动/手动完整同步 + 历史日整文件校验）、`sync_reports`（同步报告）、`delete_latest`（仅今天）、`status/admin_retry/admin_set_model` |
| `cli.py` | 交互主循环：7 个命令路由、`/v` 查看本地日记、清屏与日期解析、启动时 `full_sync` + 历史日整文件校验、维持长连接后台线程 |
| `terminal.py` | 跨平台终端输入：逐字符读取、Unicode 感知整字符退格（Windows 控制台事件 / POSIX raw），并处理后台线程通知展示 |

## 本地文件

- `credentials.example.json` 凭据**样板**：复制为 `credentials.json` 并填入服务端签发的 token
- `credentials.json` 链接凭证（服务端签发的唯一共享 token）
- `state.json` 本地同步游标（单条：当前已同步到的云端版本号）
- `outbox.json` 离线待推送队列
- `../Records/` 本地日记、`../AnalysisReports/` 云端报告副本
  （默认在 `client/` 的同级目录即项目根，与代码包 `client/` 分开放置；打包版在 exe 同级目录）

## 数据与安全

- 原始日记是唯一事实源；写后永不因同步失败回滚。
- 删改是垃圾桶语义（tombstone），不做硬删除。
- 凭证是单一共享 token（不入中枢、不入数据空间）；设备由各端自报本机名区分，每条记录带设备名。
- **加密传输是强制的**：服务端必须在 TLS 下运行；客户端 `server_url` 为 `https` 且 `verify` 指向服务端
  自签证书校验收信（自签直连，无需反向代理）。
- **连接与修改需凭证**：所有同步/修改（推送、在线删除、拉取、状态、报告、AI 管理）都要携带服务端签发
  的凭证 token；无凭证或凭证错误时只能本地记录，无法把修改传上服务端或拉取/删除云端数据。
- **无凭证/离线时照常本地记录**，上线后按 entry_id（=写入毫秒时间戳）自动合并去重。