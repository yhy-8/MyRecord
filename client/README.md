# AgentRecord 客户端（薄端）

本地优先的记录客户端：写当天日记、实时同步到服务端中枢、查看与常用命令。
**不保存模型密钥、不做本地 AI**；鉴权凭据与服务端签发。

本目录是**独立可发布的客户端工程**，不依赖 `server/`，可直接拷贝/打包运行。

## 安装

```bash
pip install -r requirements.txt
```

## 启动

首次使用前，把服务端签发的 `device_id` 与 `token` 写入 `credentials.json`（可参考
`server` 工程 `python -m server.main token create --device 名称` 签发），然后：

```bash
python -m client
```

> 入口统一为 `python -m client`（`client/__main__.py` 唤起 `client/main.py`）。
> 打包命令见仓库根 `.github/workflows/build.yml`（PyInstaller 入口 `client/__main__.py`）。

## 配置

`client/config.yaml`：服务器地址、本地数据目录（Records / AnalysisReports / Log）、
轮询与长轮询间隔。相对路径以 `client/` 为基准。本地数据目录不入服务端中枢。

## 命令

```text
/v [日期]   查看本地日记（报告请用专用 md 阅读器）
/c           清屏
/h           帮助
/d           在线删除当天最新一条（需联网，服务端确认，入垃圾桶）
/status      查看服务端 AI 自动任务状态
/retry       按队列重试服务端失败自动任务
/model       永久切换服务端 AI 模型
普通输入     立即写入当天记录并即时同步到云端
```

## 本地文件

- `credentials.json` 凭据（首登写入）
- `seq.json` 本设备单调序号（entry_id 生成）
- `outbox.json` 离线待推送队列
- `Records/` 本地日记、`AnalysisReports/` 云端报告副本、`Log/` 本地日志

## 数据与安全

- 原始日记是唯一事实源；写后永不因同步失败回滚。
- 删改是垃圾桶语义（tombstone），不做硬删除。
- 模型密钥只在服务端；传输建议启用 HTTPS。