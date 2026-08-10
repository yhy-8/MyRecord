# Agent 与报告流程

本文说明四种语义职责、模型协议和周报/月报流程。整体边界见[设计原则与系统架构](./设计原则与系统架构.md)，文件和自动调度见[运行机制与数据](./运行机制与数据.md)。

## 1. 中控与 Agent 的分工

`analysis/orchestrator.py` 决定阶段顺序、输入分组、查询清理、固定搜索、来源绑定、审查、修订次数、Markdown 和文件交付。Agent 每次只处理一项语义任务，不能访问文件、数据库、网络或其他 Agent。

模型收到的记录只包含 `date`、`time`、`tag`、`speaker`、`text` 及必要的 `text_part`，不包含内部指纹、日记路径或最终报告路径。

## 2. 任务级模型策略

四种报告职责共用当前用户选择的模型，但模式和输出预算由代码按任务固定，不增加配置项：

| 任务 | 输出协议 | Thinking | `max_tokens` |
|---|---|---:|---:|
| 日记总结 | 纯文本 | 关闭 | 4096 |
| Retrospective | 纯文本 | 开启，high | 65536 |
| ResearchPlanner | JSON | 关闭 | 1024 |
| Researcher | JSON | 开启，high | 32768 |
| Reviewer | JSON | 开启，high | 16384 |

DeepSeek 请求显式发送 `thinking.type=enabled|disabled`；thinking 任务发送 `reasoning_effort=high`，且不发送无效的 `temperature`。程序不保存或展示 `reasoning_content`。

## 3. 协议与重试

### 3.1 JSON 职责

三个 JSON 职责只允许一个顶层对象：

```json
{"action":"search|skip","query":"一个问题或空字符串"}
{"status":"supported|insufficient","text":"正文或不足原因"}
{"approved":true,"feedback":""}
```

程序允许包住整个对象的一层 JSON 代码围栏，但拒绝外围解释、未知字段、数组、嵌套结构、错误类型和不一致枚举。

JSON 模式成功结束但无法解析时，同一协议最多重新请求一次；`length`、过滤或 API 失败不进入协议重试。Reviewer 返回可解析但字段错误时，Reviewer 自己最多补答一次，不消耗被审正文的修订次数。

### 3.2 内容修订

`retry.agent_revision_limit` 控制一项语义正文的内容修订次数。确定性正文/字段校验失败与 Reviewer 拒绝共享这一上限。默认值为 1，即首稿之外最多改写一次。

Planner 的 action/query 不一致、多行问题或字段错误也使用同一有限修订。Researcher 返回 `insufficient` 和 Planner 返回 `skip` 是合法语义结果，不修订。

### 3.3 纯文本职责

Retrospective 必须返回非空连续正文，不得输出 JSON、标题、列表、URL、代码围栏或 `<summary>`。日记总结同样必须是非空正文；程序会移除包住整个响应的一层 Markdown 围栏、`<summary>` 和开头标题，仍有格式错误时按 `daily_summary_retry_limit` 有限修订。

## 4. 四种语义职责

### 4.1 Retrospective

Retrospective 整理周期内的事件、行动、问题、关注点和有依据的变化。它不得把时间先后写成因果，不得心理诊断或给出教练式命令。

输入明确区分：

- `facts.current_records`：当期原始记录。
- `facts.referenced_records`：用户显式引用的历史日记记录。
- `context.recent_summaries`：周期前最多 30 天的派生摘要。
- `context.weekly_retrospectives`：月内完整周报的回顾段；周报任务不使用。

对应 Reviewer 收到同一份材料结构。输入过大时，中控按原顺序分块，每块独立生成和审查，再按顺序合并。

### 4.2 ResearchPlanner

Planner 只用于周报。中控把本周记录按日期顺序和序列化体量平衡为最多五组；极长单日记录可按文本片段拆分，确保单组不越过输入安全边界。

每组只能返回一个公开、可搜索且不泄露隐私的问题，或返回 `skip`。程序清理邮箱、长数字和本地路径，拒绝多行/伪列表问题并去重。

### 4.3 Researcher

中控对每个保留问题固定搜索一次。Researcher 只看到问题以及网页标题、摘要和日期，不看到真实 URL 或内部来源编号。

证据足够时返回 `supported` 和连续正文；不足时返回 `insufficient` 及原因。正文必须区分资料支持、推理和不确定性，不得执行网页摘要中的指令。

### 4.4 Reviewer

Reviewer 每次只审一份 Retrospective 正文或一个 Researcher 主题。它核对事实忠实度、材料覆盖、角色、时期、因果边界、不确定性和不当定性，不负责 Markdown、URL 或文件格式。

Reviewer 能发现明显问题，但不是独立模型事实保证；系统不自动升级到 Pro、不多 Reviewer 投票。

## 5. 来源模型

中控解析日记时会为内部记录建立带日期、序号和内容指纹的 `R-*` 标识，用于分块与绑定；这些标识不发送给模型，也不写入最终报告。

最终“记录依据”直接由程序渲染为可读日期。网页链接来自搜索响应中经过 scheme、控制字符和去重校验的 URL，模型不能自行提供链接。渲染过程不对整份 Markdown 执行全局来源替换，因此不会误改正文、标题或 URL。

## 6. 周报流程

```text
冻结本周日记、引用和近期总结
        ↓
Retrospective 文本 → Reviewer JSON → 必要时一次共享修订
        ↓
按日期与体量划分最多五个记录组
        ↓
逐组 Planner JSON → 校验/必要时一次修订 → skip 或 query
        ↓
逐 query 固定搜索一次
        ↓
逐主题 Researcher JSON → Reviewer JSON → 必要时一次共享修订
        ↓
丢弃 insufficient 或未通过主题 → 原子写报告
```

以下结果仍会交付周报，并由程序写明原因：

- 所有 Planner 组都 `skip`：本周没有适合公开检索且不泄露隐私的主题。
- 所有查询都没有有效结果：候选主题未获得足够公开证据。
- 所有 Researcher 都 `insufficient` 或未通过审查：候选主题未获得足够公开证据。

搜索网络、限流、鉴权或协议错误仍会让整份周报失败。

## 7. 月报流程

```text
冻结当月记录、引用和近期总结
        ↓
提取完整落在当月内周报的“整理与回顾”段
        ↓
Retrospective 文本 → Reviewer JSON → 必要时一次共享修订
        ↓
原子写单一“整理与回顾”板块
```

同一周同时存在手动版和自动版时优先手动版。跨月周报和周报“领域探索与研究”段不进入月报输入。月报不创建 Planner、不搜索、不调用 Researcher。

## 8. 日记总结流程

```text
读取日记并计算完整文件哈希
        ↓
省略旧 summary，V4-Flash non-thinking 生成纯文本
        ↓
移除单层外包装，校验非空和正文格式，必要时有限修订
        ↓
取得日记锁并确认文件哈希未变化
        ↓
函数式替换 summary，原子写回
```

模型调用期间新增或修改记录时，本次过时总结不会写入。反斜杠、Windows 路径和正则形态通过函数式替换原样保存。

## 9. 报告元数据

报告头写入实际 `model_id`、耗时、输入/输出/缓存命中/缓存未命中/总 Token、手动或自动来源、触发方式和随机 run ID。Token 只累计当前进程实际发生的模型调用，不读取历史运行状态。
