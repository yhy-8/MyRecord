# AgentRecord

AgentRecord 是一个本地优先的个人记录与回顾工具。你可以像写日记一样自由记录，不需要预先分类，也不会把记录过程变成与 AI 对话。

它可以帮助你：

- 保存每天的 Markdown 日记。
- 自动生成日记总结。
- 生成包含整理回顾和领域探索的自然周报。
- 生成专注于长期回顾的自然月报。

## 启动

首次使用源码版时安装依赖：

```bash
pip install -r requirements.txt
```

然后运行：

```bash
python main.py
```

Windows 打包版直接运行：

```powershell
AgentRecord.exe
```

Windows 版的 `AgentRecord.exe`、`AgentRecordBackground.exe` 和 `config.yaml` 需要放在同一目录中。

## 基本使用

启动后默认进入记录模式。输入普通文字并按回车，就会保存到当天日记。

常用命令：

```text
/h                         显示帮助
/mode                      切换记录模式和报告模式
/v [日期]                  查看日记
/ref [日期]                引用以前的日记
/d                         删除今日最后一条记录
/c                         清空终端显示
```

进入报告模式后可以使用：

```text
/status                    查看自动任务状态
/s [日期]                  生成日记总结
/a weekly [日期]           生成自然周报
/a monthly [日期]          生成自然月报
/retry                     重试失败的自动任务
/m                         切换模型
```

日期可以使用 `today`、`昨天`、`-1`、`MM-DD` 或 `YYYY-MM-DD` 等写法。

## 初次配置

打开根目录中的 [`config.yaml`](./config.yaml)：

1. 填写所用模型的 `api_key`。
2. 确认 `current_model` 指向需要使用的模型。
3. 如需生成周报，启用并填写 `third_search` 搜索配置。

日记、报告和日志默认保存在：

```text
Records/
AnalysisReports/
Log/
```

## 自动任务

安装自动任务：

```bash
python main.py --install-automation
```

Windows 打包版使用：

```powershell
AgentRecord.exe --install-automation
```

启动程序后，可以在报告模式执行 `/status` 查看状态。

卸载自动任务：

```bash
python main.py --uninstall-automation
```

```powershell
AgentRecord.exe --uninstall-automation
```

移动程序目录或更换 Python 环境后，需要重新安装自动任务。

## 更新

更新前先卸载自动任务，并确认当前没有报告正在生成。

源码版更新可以替换项目根目录中的 `AgentRecord/` 代码目录。请保留自己的 `config.yaml`、`Records/`、`AnalysisReports/` 和 `Log/`；更新完成后重新安装自动任务。

Windows 打包版需要同时替换 `AgentRecord.exe` 和 `AgentRecordBackground.exe`，并保留自己的 `config.yaml` 和数据目录。

## 更多说明

需要了解内部设计或开发细节时，请阅读：

- [设计原则与系统架构](./Docs/设计原则与系统架构.md)
- [Agent 与报告流程](./Docs/Agent与报告流程.md)
- [运行机制与数据](./Docs/运行机制与数据.md)
