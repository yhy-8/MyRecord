# AgentRecord

AgentRecord 是一个本地优先的个人记录、整理回顾与领域研究系统。用户每天自由记录事实、行动、观点、理念、理想、问题和兴趣，程序不要求预先分类，也不把记录过程变成与 AI 对话。

系统自动生成日记总结、每日人物画像更新、每日信息简报、自然周报和自然月报。周报、月报包含两个独立板块：

1. **整理与回顾**：根据可追溯记录整理经历、观点、理念、理想和行为模式。
2. **领域探索与研究**：从记录或信息线索中选择少量公开问题，联网查证、分析和推演。

原始 Markdown 日记是唯一事实源。跨周期人物画像及反馈保存在可直接阅读和备份的 `AnalysisReports/Profile.md`；SQLite 只保存可重建的运行审计、来源索引、Agent 遥测和阶段缓存。更完整的产品与实现约束见 [Docs](./Docs/README.md)。

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
/f                         认可、否决或修正人物画像
/m                         切换总结和报告使用的模型
```

`/a` 默认等同于 `/a weekly`。手动报告在后台工作线程中生成；当前窗口可继续记录，但任务完成前不要关闭窗口。`/f` 只影响以后的分析，不改写已有报告。

## 配置

配置项、默认值和注释见应用目录中的 [`config.yaml`](./config.yaml)。初次使用需填写模型密钥并确认 `current_model`；启用每日信息简报或周报/月报时还必须配置 `third_search`，因为这些严格流程需要中控对每条查询和来源进行审计。相对目录以配置文件所在目录为基准。配置模板带 `config_version`；旧版 DeepSeek 官方配置缺少 `json_mode` 或 `max_tokens` 时程序会应用窄兼容默认值并明确警告，但更新时仍应把新模板字段合并进实际运行目录，而不是只替换源码目录中的示例。

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

同一输入的内容、JSON 或审查失败不会无限重跑：日总结、人物画像和周/月报告最多自动执行首次尝试和一次整点重试；报告生成阶段内部把结构修复与实质内容修订分开计数，最多两次结构修复和一次 Reviewer 内容修订。每日信息简报内部最多三次模型调用，内容失败首次即暂停自动请求。日记输入、模型或搜索配置变化后会自动解锁，也可随时用 `/retry` 明确重试；同输入重试会复用已经完成的每日信息阶段和搜索证据。网络和限流故障仍按 5 分钟重试。

自动任务严格按“昨日日记总结 → 昨日人物画像 → 今日信息简报 → 上周报告 → 上月报告”执行。缺失任务的原日期或周期会进入持久队列；前项失败或仍在等待重试时，后项不会越过它生成，跨日恢复也不会改用新的日期；`/retry` 遵循相同顺序。`/status` 会显示仍在排队的目标。

### 更新或迁移

更新运行目录前，先用旧目录中的程序卸载自动任务，确认当前报告任务已经结束，再替换代码；保留 `Records/`、`AnalysisReports/Profile.md`、分析数据库和含真实密钥的 `config.yaml`，并把新版配置模板中的新增字段合并到实际配置。替换完成后，从新目录重新安装自动任务。不要在自动任务仍启用时直接覆盖正在使用的源码目录。

从上一版首次启动时，程序会先用 SQLite Backup API 保存 `.analysis.pre-profile-markdown.sqlite3`，把已完成运行中的有效画像历史与用户反馈迁移到 `Profile.md`，再从工作数据库移除旧画像表。迁移可重复执行且不会覆盖冲突的 Markdown 内容；完成并核对备份前不要删除旧备份。

## 数据与文件

```text
Records/YYYY-MM-DD.md
AnalysisReports/
  Profile.md
  .analysis.sqlite3
  .analysis.pre-profile-markdown.sqlite3  # 仅旧画像数据库迁移时生成
  .automation-state.json
  Information/YYYY-MM-DD.md
  Weekly/YYYY-MM-DD_to_YYYY-MM-DD_manual.md
  Weekly/YYYY-MM-DD_to_YYYY-MM-DD_auto.md
  Monthly/YYYY-MM_manual.md
  Monthly/YYYY-MM_auto.md
Log/AgentRecord.log
```

## 构建 Windows EXE

GitHub Actions 在每次 `push` 时自动构建 Windows 产物，也支持手动触发。

```powershell
pyinstaller --onefile --name AgentRecord main.py
pyinstaller --onefile --noconsole --name AgentRecordBackground main.py
Copy-Item config.yaml dist\config.yaml
```

最终分发 `dist` 中的两个 EXE 和 `config.yaml`。更新时必须同时替换两个 EXE，并重新安装自动任务。
