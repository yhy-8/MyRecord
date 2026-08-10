# AgentRecord

AgentRecord 是一个本地优先的个人记录、整理回顾与领域研究系统。用户每天自由记录事实、行动、观点、理念、理想、问题和兴趣，程序不要求预先分类，也不把记录过程变成与 AI 对话。

系统自动生成日记总结、自然周报和自然月报：

1. **周报**包含“整理与回顾”和“领域探索与研究”两个独立板块；探索主题只从本周记录引出，并联网查证、分析和推演。
2. **月报**只做整理与回顾，可参考完整落在当月内的周报，不重复进行领域探索。

原始 Markdown 日记是唯一事实源。报告阶段只在当前进程内存中流转，通过审查后原子写入 Markdown；程序不使用 SQLite，也不跨运行复用旧模型正文或搜索证据。进一步了解项目时，可分别阅读[设计原则与系统架构](./Docs/设计原则与系统架构.md)、[Agent 与报告流程](./Docs/Agent与报告流程.md)和[运行机制与数据](./Docs/运行机制与数据.md)。

## 启动

Linux 或其他类 Unix 环境：

```bash
python main.py
```

也可以通过包入口启动：

```bash
python -m AgentRecord
```

Windows 打包版：

```powershell
AgentRecord.exe
```

Windows 版 `AgentRecord.exe`、`AgentRecordBackground.exe` 和 `config.yaml` 应放在同一目录。后台专用程序没有控制台窗口，不要单独启动。

Windows 交互终端使用按键事件等待和批量 Unicode 回显，不再通过固定休眠轮询按键。更新时需同时替换两个 EXE，否则可能仍在运行旧版输入逻辑。

## 记录模式

启动后默认进入记录模式。普通文字按回车后立即写入当天日记，不等待模型。

```text
/h                         显示记录模式帮助
/mode                      切换到报告模式
/v [日期]                  查看日记
/ref [日期]                选择并引用日记
/d                         删除今日最后一条记录
/c                         清空终端显示
```

日期支持 `-1`、`today`、`昨天`、`MM-DD` 和 `YYYY-MM-DD` 等写法。新记录只能引用日记，不能引用报告。

## 报告模式

执行 `/mode` 后进入报告模式：

```text
/h                         显示报告模式帮助
/mode                      返回记录模式
/status                    查看自动任务产物、进度与失败状态
/s [日期]                  手动生成日记总结
/a weekly [日期]           手动生成自然周报
/a monthly [日期]          手动生成自然月报
/retry                     后台按依赖顺序重试失败自动任务
/m                         切换总结和报告使用的模型
```

`/a` 默认等同于 `/a weekly`。手动报告在后台工作线程中生成；当前窗口可继续记录，但任务完成前不要关闭窗口。

## 配置

配置项、默认值和注释见应用目录中的 [`config.yaml`](./config.yaml)。初次使用需填写活动模型密钥并确认 `current_model`；启用周报时还必须配置 `third_search`，月报不联网。`retry` 统一控制 Agent 内容修订、HTTP/空响应重试、自动暂停阈值和等待间隔；其中 `*_retry_limit` 均不含首次调用。相对目录以配置文件所在目录为基准。任务级 thinking 和输出安全预算由程序按职责固定，语义正文不做生成后的字数判定。

## 自动任务

安装（可重复执行）：

```bash
python main.py --install-automation
```

```powershell
AgentRecord.exe --install-automation
```

查看状态：启动程序，用 `/mode` 进入报告模式，然后执行：

```text
/status
```

卸载：

```bash
python main.py --uninstall-automation
```

```powershell
AgentRecord.exe --uninstall-automation
```

移动程序目录或更换 Python 环境后，重新执行安装命令。

同一输入的内容、JSON 或审查失败不会无限重跑。Planner、Researcher、Reviewer 使用最小 JSON；Retrospective 和日记总结使用纯文本。JSON 无法解析时最多进行一次协议重试；已解析结果的字段/正文校验或 Reviewer 拒绝共享 `retry.agent_revision_limit` 次内容修订机会。自动任务在同一输入累计失败达到 `retry.automation_content_failure_limit` 后暂停；日记输入、模型、重试策略或周报搜索配置变化后会自动解锁，也可用 `/retry` 明确重试。报告重试会实时重新生成所有语义阶段。

自动任务严格按“昨日日记总结 → 上周报告 → 上月报告”执行。缺失任务的原日期或周期会进入持久队列；前项失败或仍在等待重试时，后项不会越过它生成，跨日恢复也不会改用新的日期；`/retry` 遵循相同顺序。`/status` 会显示仍在排队的目标。

### 更新运行目录

更新前先卸载自动任务并确认报告进程已经结束。替换代码时保留 `Records/`、`AnalysisReports/`、`Log/` 和含真实密钥的 `config.yaml`；完成验证后再安装自动任务。

## 数据与文件

```text
Records/YYYY-MM-DD.md
AnalysisReports/
  .automation-state.json
  Weekly/YYYY-MM-DD_to_YYYY-MM-DD_manual.md
  Weekly/YYYY-MM-DD_to_YYYY-MM-DD_auto.md
  Monthly/YYYY-MM_manual.md
  Monthly/YYYY-MM_auto.md
Log/AgentRecord.log
```

周报和月报文件头会直接显示模型、生成耗时及输入、输出、缓存命中、缓存未命中和总 Token。

## 构建 Windows EXE

GitHub Actions 在每次 `push` 时自动构建 Windows 产物，也支持手动触发。

```powershell
pyinstaller --onefile --name AgentRecord main.py
pyinstaller --onefile --noconsole --name AgentRecordBackground main.py
Copy-Item config.yaml dist\config.yaml
```

最终分发 `dist` 中的两个 EXE 和 `config.yaml`。更新时必须同时替换两个 EXE，并重新安装自动任务。
