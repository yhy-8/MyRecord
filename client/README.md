# MyRecord 客户端（薄端）

本地优先的记录客户端：写当天日记、同步到服务端中枢、查看与常用命令。
**不保存模型密钥、不做本地 AI**；鉴权凭据由服务端签发。本目录是**独立可发布的客户端
工程**，不依赖 `server/`，可直接拷贝/打包运行。

## 交互入口

```bash
python -m client
```

> 入口统一为 `python -m client`（`client/__main__.py` 唤起 `client/main.py`）。
> 打包命令见仓库根 `.github/workflows/build.yml`（PyInstaller 入口 `client/__main__.py`）。

## 同步模型（与云端的协作方式）

客户端**不密集轮询云端**，按以下三条规则与中枢协作：

1. **启动时一次性完整同步**：连上云端，拉取对账（补齐缺失、按删除标记移除）、冲刷离线
   队列、同步报告。
2. **运行期间保持一条长连接（长轮询）**：`/api/sync/longpoll` 一直挂起，服务端有更新
   （扇出）即返回并立即应用；超时立即重新挂起，等效于一条持续的"长链路"。每条记录
   写入即触发 push（写后即时同步），方便、低开销。
3. **手动同步用 `/sync`**：随时再完整同步一次（推队列 + 拉取 + 同步报告）。

客户端只在前台交互运行时保持长连接；关闭程序即无任何后台任务。断网时本地照常记录并
进 `outbox.json` 离线队列，恢复后由启动同步 / 长连接回连 / `/sync` 冲刷补齐。

## 配置

`client/config.yaml`：服务器地址、本地数据目录（Records / AnalysisReports / Log）、
长轮询挂起秒数。相对路径以 `client/` 为基准。本地数据目录不入服务端中枢。
（已移除旧的每 1 分钟定时轮询间隔。）

## 命令（统一 8 个）

```text
/v [日期]   查看本地日记（报告请用专用 md 阅读器）
/c           清屏
/h           帮助
/sync        立即手动完整同步一次（推送离线队列、拉取对账、同步报告）
/d           在线删除当天最新一条（需联网，服务端确认，正文入垃圾桶）
/status      查看服务端 AI 自动任务状态
/retry       按队列重试服务端失败自动任务
/model       永久切换服务端 AI 模型
普通输入     立即写入当天记录并即时同步到云端
```

## 代码总览（每个文件做什么）

| 文件 | 职责 |
|---|---|
| `main.py` / `__main__.py` | 程序入口：`python -m client` → `run_interactive()` |
| `config.py` / `config.yaml` | 读取本地配置（服务器地址、数据目录、长轮询秒数） |
| `credentials.py` | 读取/写入 `credentials.json`（服务端签发的 device_id/token） |
| `idseq.py` | 本设备单调序号（`seq.json`），生成 `entry_id = device_id-序号` |
| `journal.py` | 本地日记写入：按天 `Records/YYYY-MM-DD.md` 原子追加、对账补齐、tombstone 移除 |
| `render.py` / `terminal.py` | 日记渲染格式 / 清屏、日期解析辅助 |
| `file_lock.py` | 跨进程互斥（`.journal.lock` 等），保证原子写 |
| `sync.py` | 与中枢的同步客户端：`push_new`（写后即 push）、`send_pending`（冲刷离线队列）、`pull`（拉取对账）、`longpoll`（长连接扇出）、`full_sync`（启动/手动完整同步）、`sync_reports`（同步报告）、`delete_latest`、`status/admin_retry/admin_set_model` |
| `cli/app.py` | 交互主循环：八个命令路由、启动时 `full_sync`、维持长连接后台线程 |
| `cli/view.py` | `/v` 查看本地日记 |

## 本地文件

- `credentials.json` 凭据（首登写入）
- `seq.json` 本设备单调序号（entry_id 生成）
- `outbox.json` 离线待推送队列
- `Records/` 本地日记、`AnalysisReports/` 云端报告副本、`Log/` 本地日志

## 数据与安全

- 原始日记是唯一事实源；写后永不因同步失败回滚。
- 删改是垃圾桶语义（tombstone），不做硬删除。
- 模型密钥只在服务端；传输建议启用 HTTPS。