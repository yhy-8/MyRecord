# AgentRecord 服务端（中枢 + 云端 AI）

数据中枢与云端 AI：设备鉴权、条目合并/对账/扇出、垃圾桶、自动任务（日总结、自然周报、
自然月报）。模型密钥只存这里。日志不保存私密原文。

本目录是**独立可发布的服务器工程**，不依赖 `client/`，可直接拷贝/部署运行。

## 安装与启动

```bash
pip install -r requirements.txt
python -m server.main run
```

> 入口统一为 `python -m server.main run`（`server/main.py`）。
> 启动后自带每分钟后台线程按“日总结 → 周报 → 月报”依赖顺序执行自动任务。

## 子命令

```text
python -m server.main run            启动同步+AI 服务
python -m server.main token create --device 名称   签发新设备令牌（首登用）
python -m server.main token list     列出活动设备
python -m server.main token rotate --device 名称   轮换令牌
python -m server.main token revoke --device 名称   停用设备
python -m server.main import --records 路径    导入旧版 Records
python -m server.main render          重渲染当天 Records
```

## 配置

`server/config.yaml`：监听地址/端口、`models`、`current_model`、`retry`（失败/重试策略）、
`third_search`（周报联网搜索）、`automation`（开关）、数据目录（`data_dir`、`diary_dir`、
`analysis_dir`、`log_dir`，相对路径以 `server/` 为基准）。

## 部署

`server/deploy/`：

- `agentrecord-server.service` — systemd 单元（安装/启停说明见文件头注释）。
- `backup.sh` — 备份 `data` 空间为 tar（保留最近 N 份）。
- `restore.sh` — 从备份恢复 `data` 空间。

数据空间（运行时生成）：`server/data/` — `state.json`（权威条目/设备/垃圾桶）、
`Records/`（渲染每日日记）、`Trash/`（被删正文）、`AnalysisReports/`（报告与自动任务状态）、
`Log/`（服务端日志）。

## 数据与安全

- 原始日记是唯一事实源；报告、总结、自动任务状态都是可重建派生数据。
- 删改为垃圾桶语义（tombstone），不做硬删除。
- 设备鉴权用长令牌，服务端只存哈希（scrypt）。
- 模型密钥只在服务端；不入数据空间、不入日志。