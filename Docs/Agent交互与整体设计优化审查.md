# AgentRecord Agent 交互与整体设计优化审查

> 审查日期：2026-08-09  
> 审查对象：当前 `main` 分支、`config.yaml`、三份现有设计文档、Agent/报告/自动任务实现及测试  
> 目标模型：以 `deepseek-v4-flash` 为主力，`deepseek-v4-pro` 仅保留为用户主动切换项  
> 本文是设计审查与优化建议，不代表这些改动已经实现。

## 1. 结论摘要

当前产品边界是合理的：用户可见的 AI 能力只保留日记总结、周报、月报三类，周报负责回顾和探索，月报只负责总结。中控掌握文件、日期、编号、来源、搜索、重试和 Markdown，AI 只处理语义，这个大方向应继续保持。

需要调整的不是增加更多 Agent，而是进一步缩短生成链路、明确失败语义，并让 SQLite 完全退出报告生成的输入路径。

本次审查的推荐结论如下：

1. **取消所有跨运行语义阶段缓存。** Retrospective、Planner、Researcher、Reviewer 和搜索证据都不应从 SQLite 读回并进入新报告。
2. **建议最终移除 SQLite。** 当前数据库没有用户界面消费者；移除缓存读取后，它只剩不可见的内部审计，而正式报告、自动状态和日志已经覆盖实际产品需要。
3. **保留单次运行内的内存结果。** 同一主题修订时不重复搜索，同一正文复审时不重建上下文；进程退出后不复用。
4. **依靠 DeepSeek 官方输入缓存降低重复请求成本。** 官方缓存只复用输入前缀，输出仍重新推理，不会让本地历史正文决定新报告。
5. **JSON 只用于“决策＋正文”协议。** Planner、Researcher、Reviewer 保留最小 JSON；日记总结和 Retrospective 直接返回文本，不必用 `{"text":"..."}` 包一层。
6. **不采用自动模型升级。** V4-Flash 足以承担当前任务；失败后自动切 Pro 会改变成本和输出语义，应该由用户主动 `/m` 切换。
7. **每项语义任务最多一轮内容修订。** 不增加 JSON 修复 Agent、反复自我纠错或多模型投票。
8. **“没有合适研究主题”应是合法周报结果，不应让整份周报失败。** 搜索服务故障仍应失败；内容本身不值得搜索则应诚实落空。

最终推荐架构是：原始文件和辅助材料构造不可变输入快照，中控按固定顺序调用少量语义任务，结果只在内存中流转，通过审查后直接原子写 Markdown；自动队列继续保存在 `.automation-state.json`，诊断继续使用有界日志，不再需要报告数据库。

## 2. 审查基线与模型事实

### 2.1 当前模型能力

DeepSeek 官方将 V4-Flash 定位为快速、经济的主力模型，并称其推理能力接近 V4-Pro、简单 Agent 任务表现与 Pro 相当。V4-Flash 和 V4-Pro 都支持 1M 上下文、JSON Output、工具调用及 thinking/non-thinking 双模式；官方当前给出的最大输出是 384K。[DeepSeek V4 发布说明](https://api-docs.deepseek.com/news/news260424/)、[模型与价格](https://api-docs.deepseek.com/quick_start/pricing)

官方价格显示，V4-Flash 每百万输入 Token 的缓存未命中价格为 0.14 美元、缓存命中为 0.0028 美元，输出为 0.28 美元。对 AgentRecord 这种低频个人报告而言，重新执行一次语义阶段的成本很低，通常不足以抵消本地跨运行缓存带来的状态、兼容和可信性复杂度。[模型与价格](https://api-docs.deepseek.com/quick_start/pricing)

### 2.2 Thinking 模式

V4 默认开启 thinking，普通 thinking 请求默认使用 high effort。Thinking 模式不使用 `temperature`、`top_p` 等采样参数；设置这些参数不会报错，但不会生效。[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode)

当前 `ai_client.py` 没有显式发送 thinking 开关，因此所有任务实际继承服务端默认值。这意味着日总结和 Planner 这类简单任务也在使用 high thinking，增加了延迟和推理消耗，却没有对应收益。

目标设计应由程序按任务固定选择模式，而不是再增加用户配置：

| 任务 | 推荐模式 | 原因 |
|---|---|---|
| 日记总结 | non-thinking | 单日压缩任务，输入边界清楚，不需要复杂推演 |
| Retrospective | thinking / high | 需要跨记录综合、区分事实与变化，正确性优先 |
| ResearchPlanner | non-thinking | 每组只做 search/skip 和单问题提炼，属于简单 Agent 任务 |
| Researcher | thinking / high | 需要比较证据、反例、边界与不确定性 |
| Reviewer | thinking / high | 需要从原始材料发现遗漏、错误归因和推理越界 |

不应保存或展示 `reasoning_content`。当前各阶段都是独立单轮调用，没有需要继续工具调用的多轮 reasoning 链，最终正文和使用量已经足够。

### 2.3 JSON Output 的真实边界

DeepSeek JSON Output 要求请求设置 `response_format={"type":"json_object"}`，提示中明确出现 JSON 并给出目标格式。官方保证返回合法 JSON 字符串，但同时提示可能偶发空 content，并建议合理设置 `max_tokens` 防止中途截断。[JSON Output](https://api-docs.deepseek.com/guides/json_mode/)

“合法 JSON”不等于“符合业务字段契约”。官方 API 文档同样提醒，即使是工具参数也仍应由程序验证。因此当前的字段集合、类型、枚举和正文校验不能删除。[Chat Completion API](https://api-docs.deepseek.com/api/create-chat-completion)

DeepSeek 还提供基于工具调用的 strict JSON Schema，但它目前需要 Beta base URL，而且工具调用本身不符合本项目“模型不调用工具、只返回语义结果”的边界。为了三个简单对象引入 Beta 接口和工具协议，会增加而不是减少复杂度，不建议采用。[Tool Calls strict mode](https://api-docs.deepseek.com/guides/tool_calls)

## 3. 当前本地缓存有什么用

当前 `AnalysisStore` 保存两类内容：

1. `analysis_runs`：一次周报或月报的运行状态、周期、模型、输入哈希和最终路径。
2. `agent_artifacts`：Retrospective、Planner、搜索证据、Researcher、Reviewer 的成功或失败结果。

其中真正影响产品行为的是“从等价失败运行读取 completed artifact”。它试图解决以下问题：

- 周报已经完成回顾，但后续搜索失败，重试时不再重新做回顾。
- Planner 和搜索完成后 Researcher 失败，重试时不重复规划或搜索。
- 减少模型 Token、第三方搜索调用和等待时间。
- 保留失败阶段响应，便于事后诊断。

这些目标在旧模型成本高、格式不稳定、长上下文昂贵时有现实意义。但在当前设计里，它们的收益已经明显下降：

- V4-Flash 成本低，并且官方输入缓存默认启用。
- 当前每个任务已经拆成单项调用，失败重做范围有限。
- 一个周报最多五个查询，单次运行内已经确保修订不会重复搜索。
- 用户没有查看 artifact 的界面，`/status` 也不读取 SQLite。
- 最终报告已经保存模型、耗时、Token、来源和 run ID；日志保存阶段状态。
- 数据库缓存会让旧正文、旧提示词契约或逻辑损坏的 payload 直接进入新报告。

### 3.1 为什么“加版本和重新校验”仍不是首选

可以给缓存加入 pipeline version、字段校验、来源校验和回退重算。这能修复主要正确性问题，但会继续保留以下成本：

- 每次提示词或语义契约变化都要判断是否升级版本。
- 每个 Agent 都需要一套独立的缓存 payload 校验器。
- 分块 Retrospective、单主题 Researcher 和最终聚合 artifact 的关系仍要维护。
- 数据库 JSON 损坏、表结构异常和写完成状态失败仍会进入报告控制流。
- 测试必须长期覆盖新旧版本、跨运行复用和补偿回滚。

这些复杂度只为“失败后少调用几次廉价模型”服务，不符合当前项目的简单优先原则。

### 3.2 推荐决策

推荐完全取消跨运行复用：

- 不读取 Retrospective、Planner、Researcher、Reviewer artifact。
- 不读取历史搜索证据。
- 一次报告进程内继续使用内存中的主题、证据、正文和遥测。
- 报告失败后的下一次重试从输入快照重新开始。
- DeepSeek 官方输入缓存可以自动降低完全相同请求的成本，但模型仍重新生成输出。官方明确说明缓存只命中输入前缀，输出仍通过推理生成，并可能具有随机性。[Context Caching](https://api-docs.deepseek.com/guides/kv_cache)

进一步建议直接删除 SQLite：

- `run_id` 由中控直接生成 UUID。
- Token 汇总改为一个进程内字典或小型 `UsageAccumulator`。
- 阶段开始、完成、失败写入现有有界日志。
- 正式报告原子写入成功即代表交付成功。
- 数据库或审计写入失败不再删除一份已经验证并写好的报告。
- 自动任务仍以日记总结和 `_auto.md` 文件判断完成。

如果确实希望保留历史诊断，数据库也只能是 best-effort 的只写审计旁路：生成流程不得查询它，写入失败不得改变报告结果。但在没有查询界面的当前产品中，这个旁路没有足够价值。

## 4. AI 与确定性中控的合理边界

### 4.1 必须由程序完成

下列工作都有明确规则，不能交给模型：

- 确定自然日、自然周、自然月范围。
- 决定手动/自动报告路径和覆盖规则。
- 读取日记、去除同日 `<summary>`、解析标准记录。
- 验证引用只能落在 `Records` 内。
- 给记录按日期分组，限制最多五组。
- 给主题、查询和网页结果编号。
- 清理查询中的邮箱、长数字和本地路径。
- 限制查询是一行、一个问题；执行去重。
- 固定每个查询只搜索一次，验证 URL scheme、控制字符并去重。
- 决定搜索失败、无结果和证据不足分别是什么状态。
- 绑定本次输入对应的记录日期与网页链接。
- 生成所有 Markdown 标题、引用块、链接、元数据和文件名。
- 校验 JSON 字段、类型、枚举、未知字段和正文形态。
- 分类网络、限流、鉴权、截断、过滤、空响应和内容失败。
- 控制重试次数、等待时间、自动队列和前置屏障。
- 原子写日记总结与报告，保留已有正式文件。

模型不能决定数组、来源 ID、URL、路径、报告是否存在、自动任务是否完成，也不应输出最终 Markdown 文档树。

### 4.2 应由 AI 完成

AI 只负责必须理解语言含义的判断：

- 从单日日记提炼简短总结。
- 从周期记录提炼事实、关注点和有依据的变化。
- 判断一个记录组是否值得产生公开研究问题。
- 基于固定搜索摘要综合证据、反例、边界和不确定性。
- 判断一段正文是否忠于本次提供的材料。

AI 不需要知道最终路径、运行 ID、数据库、来源编号或报告模板。

### 4.3 输入中的信任层级

当前 Retrospective 能看到原始记录、引用记录、近期总结和月内周报，但 Reviewer 只看到原始和引用记录。目标设计应让生成者与 Reviewer 看到相同的可核验材料，并给材料加明确层级：

```text
facts.current_records          当期原始记录，主要事实依据
facts.referenced_records       用户显式引用的历史记录
context.recent_summaries       派生辅助上下文，不能单独证明当期事实
context.weekly_retrospectives  派生辅助上下文，只帮助月报整理
evidence.search_results        外部不可信证据，仅用于当前研究主题
```

月报最好只读取周报的“整理与回顾”正文，不把“领域探索与研究”整段重新送入 Retrospective。这样可以从程序层确保月报不会重复周报研究，同时仍保留已有整理成果。

所有提供给模型的记录对象可以去掉内部指纹 ID。模型只需要日期、时间、speaker、tag 和 text；中控已经知道整块输入对应哪些来源，减少模型复述或泄漏内部 ID 的机会。

## 5. 更合理的 JSON 使用方式

JSON 应该表达控制决策，而不是承担排版，也不必包装没有控制语义的单字符串。

### 5.1 推荐保留 JSON 的任务

ResearchPlanner：

```json
{"action":"search|skip","query":"单个问题或空字符串"}
```

这里的 `action` 能区分“有意跳过”和“模型漏写 query”，有实际协议价值。

Researcher：

```json
{"status":"supported|insufficient","text":"正文或不足原因"}
```

这里的 `status` 决定主题进入报告还是被丢弃，也有实际协议价值。

Reviewer：

```json
{"approved":true,"feedback":""}
```

这里的布尔值与反馈一致性可以由程序严格校验。

### 5.2 推荐改为纯文本的任务

日记总结继续使用纯文本。

Retrospective 目前只返回：

```json
{"text":"连续正文"}
```

这个对象没有决策字段，JSON 只增加转义、空 content 和解析失败面。建议改为纯文本响应，再由程序执行以下校验：

- 正文非空。
- 不是接口失败占位文字。
- 不包含模型生成的标题、列表或 URL。
- 不包含 `<summary>` 或代码围栏等包装。

如果未来 Retrospective 确实需要 `ok|insufficient` 等控制状态，再恢复 JSON；不要为了所有 Agent 表面统一而保留无意义字段。

### 5.3 不应加入的结构

不建议让任何 Agent 返回以下内容：

- `sections`、`paragraphs` 或 `topics` 数组。
- 来源 ID 数组或 URL 数组。
- Markdown 标题和列表。
- 主题优先级、编号或文件路径。
- 多个查询组成的嵌套对象。

多项任务继续由中控拆成多次单项调用。这是当前架构最正确的部分之一。

### 5.4 任务级输出预算

当前所有任务共享 `max_tokens=100000`。这不是内容长度判定，但对只应返回几十个字的 Planner 和 Reviewer 来说过大，也放大了官方所说的 JSON 空白输出或异常长输出风险。

建议由代码给每类任务设置宽松的安全预算，不增加用户配置项。Thinking 请求的预算必须同时容纳 reasoning 和最终正文，不能只按可见正文长度设置；官方 API 也把 reasoning tokens 计入生成使用量。[Chat Completion API](https://api-docs.deepseek.com/api/create-chat-completion)

| 任务 | 建议 `max_tokens` | 说明 |
|---|---:|---|
| 日记总结 | 4,096 | non-thinking；足以覆盖完整单日总结 |
| ResearchPlanner | 1,024 | non-thinking；只返回 action 和单个 query |
| Reviewer | 16,384 | thinking；包含推理预算，只返回决定和一段反馈 |
| Researcher | 32,768 | thinking；包含推理预算和单主题研究正文 |
| Retrospective | 65,536 | thinking；包含推理预算和单次/单分块回顾正文 |

这些值是接口异常预算，不是生成后的字数限制。若接口以 `length` 结束，仍应把响应视为未完整交付；程序不截断已完成正文。

## 6. 推荐的统一错误兜底

V4-Flash 已经足够强，不需要复杂的修复 Agent。兜底应按错误层级处理，而不是对所有失败重复同一种请求。

| 错误类别 | 建议行为 | 是否消耗内容修订 |
|---|---|---|
| 连接、超时、HTTP 408/5xx | 单请求层最多重试 2 次，指数等待 | 否 |
| HTTP 429 | 不在客户端快速重试，交给自动调度等待 | 否 |
| 401/403、缺少密钥、搜索未配置 | 立即配置阻塞，不调用其他 Agent | 否 |
| `finish_reason=length` | 当前阶段失败；检查分块或预算，不使用残缺正文 | 否 |
| `content_filter` | 当前阶段失败并明确分类 | 否 |
| `insufficient_system_resource` | 作为临时服务失败进入网络等待 | 否 |
| `stop` 但 content 为空 | 最多进行 1 次空响应补答 | 否 |
| JSON 模式下 `stop` 但无法解析 | 最多 1 次全新协议重试；再次失败即结束 | 否 |
| JSON 可解析但字段/枚举错误 | 原 Agent 最多修订 1 次 | 是 |
| 正文标题、列表、URL 等确定性违规 | 原 Agent 最多修订 1 次 | 是 |
| Reviewer 拒绝 | 原生成 Agent 修订 1 次，与确定性违规共享上限 | 是 |
| Reviewer 自身协议错误 | Reviewer 自己最多协议重试 1 次，不转嫁给生成 Agent | 否 |
| Researcher 返回 insufficient | 直接丢弃主题，不重试 | 否 |
| Planner 返回 skip | 合法结果，不重试 | 否 |
| 没有任何合适主题 | 完成周报并写确定性说明，不作为失败 | 否 |
| 搜索服务报错 | 周报失败，按网络/配置类别处理 | 否 |
| 查询成功但没有有效结果 | 丢弃该主题；全部为空时仍可完成周报 | 否 |
| 文件原子写失败 | 保留旧报告，当前运行失败，不重新调用模型 | 否 |

最多调用次数应保持可计算：每个语义生成项首稿加一次共享修订；Reviewer 首审加必要的一次协议重试；不存在无限修复、JSON 修复 Agent或自动 Pro 升级。

## 7. 三类产物的目标流程

### 7.1 昨日日记总结

```text
读取指定日记并创建原文哈希
        ↓
去掉原 summary，保留原始记录和引用角色
        ↓
V4-Flash non-thinking，纯文本总结
        ↓
非空/包装/接口完成状态校验
        ↓
重新取得日记锁并确认原文哈希未变化
        ↓
函数式替换 summary，原子写回
```

这里不需要 Reviewer、JSON、SQLite 或搜索。

当前 `journal.update_summary_for_date()` 使用模型正文作为 `re.sub` 替换字符串，Windows 路径或反斜杠可能被解释成正则分组引用。目标实现必须改成函数式替换。对手动总结当天日记，还应在写回前确认模型调用期间原始记录没有变化，避免写入过时总结。

### 7.2 周报

```text
冻结本周记录、引用和近期总结输入
        ↓
Retrospective 文本 → Reviewer JSON → 必要时一次修订
        ↓
中控按日期和输入大小划分最多五组
        ↓
每组 Planner JSON → 校验/一次修订 → skip 或一个 query
        ↓
中控清理、去重并逐查询固定搜索一次
        ↓
每主题 Researcher JSON → Reviewer JSON → 必要时一次修订
        ↓
丢弃 insufficient 或仍未通过的主题
        ↓
中控渲染两个固定板块并原子写报告
```

关键调整：

- Planner 的字段错误也必须获得与其他 Agent 一致的一次修订机会。
- 分组除了连续日期，还应按序列化字符量尽量平衡，避免某一天极长导致单组超过输入预算。
- 没有查询、没有有效搜索结果或所有主题 evidence insufficient，不应让整份周报反复失败。研究板块可以由程序写入：“本周记录没有产生适合公开检索且不泄露隐私的探索主题。”或“本周候选主题未获得足够公开证据。”
- 只有搜索服务本身网络、鉴权或协议失败才应让周报失败。
- 同一次报告内，Researcher 修订继续复用内存中的搜索证据；下一次报告重试重新搜索。

### 7.3 月报

```text
冻结当月记录、引用、近期总结
        ↓
读取当月完整周报中的“整理与回顾”部分作为派生上下文
        ↓
Retrospective 文本 → Reviewer JSON → 必要时一次修订
        ↓
中控渲染单一“整理与回顾”板块并原子写报告
```

月报不创建 Planner，不搜索，不调用 Researcher，不扩展周报研究。近期总结和周报回顾允许帮助理解连续性，但输入结构和提示词必须说明它们是派生上下文；Reviewer 应看到相同材料，才能判断正文是否把历史或派生内容误写成当月原始事实。

## 8. Reviewer 是否有必要

在当前任务中保留 Reviewer 是合理的，但应清楚它的能力边界：

- 同一模型的独立审查能发现明显遗漏、无依据因果、角色混淆和格式越界。
- 它不能提供真正独立于生成模型的事实保证，错误可能相关。
- 不需要用 Pro 自动审查 Flash，也不需要多 Reviewer 投票。
- Reviewer 只负责语义忠实度，不检查 Markdown、URL、编号和文件。
- Reviewer 必须看到生成正文实际允许使用的全部材料，并带信任层级。
- Reviewer 通过不应成为缓存正文跨版本复用的许可证。

推荐保留两类审查：Retrospective 审查和逐主题 Researcher 审查。Planner 只需要程序字段校验和一次修订，不需要额外 Reviewer；日记总结也不需要 Reviewer。

## 9. 推荐目标架构

```text
Records/ + 已交付周报
          │
          ▼
analysis/context.py
  解析、边界、引用、辅助材料、不可变输入快照
          │
          ▼
analysis/orchestrator.py
  固定阶段顺序、单项分派、内存状态、审查、渲染
      │                     │
      ▼                     ▼
agents/*.py             ai_client.py
语义契约与校验          HTTP、thinking、JSON、遥测、错误分类
      │                     │
      └──────────┬──────────┘
                 ▼
       原子写 Weekly/Monthly Markdown

automation.py
  只维护缺失检测、持久队列、前置屏障和重试时间

Log/AgentRecord.log
  只保存阶段、错误类别、耗时和 Token，不保存私密原文
```

删除 `analysis/store.py` 后，当前两个最大模块仍然偏长，但先完成状态删除，再判断是否拆分：

- `orchestrator.py` 可以保留为一条可线性阅读的报告流水线。
- `automation.py` 如果仍超过合理规模，可只把 Windows/Linux 安装逻辑拆到 `scheduler_install.py`；不要把任务状态机拆成多个抽象层。
- `UsageAccumulator` 应是几十行以内的进程内工具，不重新创建存储层。

## 10. 当前实现保留与调整清单

### 10.1 应保留

- 三类产物及周报/月报边界。
- 四个内部语义职责的概念分离。
- Planner 和 Researcher 的单项调用方式。
- JSON 顶层对象、精确字段集合、拒绝未知字段。
- 中控生成 Markdown、编号、URL、路径和来源。
- 搜索结果对 Researcher 隐藏真实 URL。
- 原始记录引用目录穿越防护。
- 一次共享内容修订。
- 搜索在单次运行内只执行一次。
- 自动任务文件队列、准确目标和严格依赖顺序。
- 手动/自动报告独立路径和原子替换。

### 10.2 应调整

- 显式按任务设置 thinking 模式和输出预算。
- Retrospective 改纯文本。
- Planner 加字段校验修订循环和失败记录。
- Reviewer 协议错误单独补答，不消耗正文修订。
- Reviewer 接收与生成者一致的核验材料。
- 月报只读取周报回顾部分。
- 无研究主题/证据不足改为合法空探索，不再阻塞月报。
- 来源日期在渲染时直接生成，不对整份 Markdown 全局正则替换。
- 记录依据优先显示可读日期或相对日记链接，而不是 `R-20260714`。
- 日总结使用安全替换，并在写回前验证输入未变化。
- 启动时主动警告活动模型密钥为空、自动周报启用但搜索不可用。
- 报告 Token 元数据补充缓存未命中量。

### 10.3 应删除

- 跨运行 `reusable_artifact()` 及所有缓存命中分支。
- `agent_artifacts` 与 `analysis_runs`，若采纳无 SQLite 方案。
- 报告写完后因 SQLite 完成状态失败而删除/恢复报告的补偿逻辑。
- `journal.read_daily_log()`、`search_history()` 等没有生产调用者的旧工具接口，或让 `/v` 统一调用其中一个后只保留一份实现。
- `ToolResult.content` 和当前无人消费的搜索文本拼装。
- `_research_section()` 单层转发和已无用途的 `current_source_ids` 参数。
- `analysis.__init__` 中未公开也未使用的私有自动报告导入。

旧自动状态中的 `daily_profile`、`daily_information` 清理代码可以暂时保留一个明确升级周期；到期后一次性删除，不继续扩展兼容层。

## 11. 实施顺序

建议拆成四个可独立验证的变更，不进行一次性重写。

### 第一阶段：修复确定性错误

- 修复日总结反斜杠替换。
- 把来源缩写移到记录依据渲染，不再全局改 Markdown。
- 补 Planner 一次修订。
- 补配置前置警告和缓存未命中 Token 显示。

这一阶段不改变报告语义和数据库结构，风险最低。

### 第二阶段：取消缓存读取

- 删除所有 `reusable_artifact()` 调用。
- 搜索、主题和正文只在当前运行内存中复用。
- 保留数据库写入一小段过渡期，确认报告重试、成本和日志足够。
- 增加测试，证明向 SQLite 写入任意 payload 都不会改变新报告。

### 第三阶段：移除 SQLite

- 把 Token 累计移到内存。
- 中控直接生成 run ID。
- 报告文件交付成为唯一完成条件。
- 删除 `store.py`、数据库测试和相关文档。
- 确认没有报告进程后，旧 `.analysis.sqlite3`、`-wal`、`-shm` 可由用户删除；现有报告和日记不受影响。

### 第四阶段：精简 Agent 协议

- Retrospective 改纯文本。
- 显式设置任务级 thinking 和 `max_tokens`。
- 统一协议重试与内容修订计数。
- 调整月报辅助上下文和周报合法空探索。
- 最后删除确认无调用者的旧函数和转发层。

## 12. 完成标准与测试矩阵

目标设计至少应增加以下测试：

### 数据库独立性

- 数据库不存在、损坏或包含伪造 artifact 时，新报告正文完全相同地走实时生成流程。
- 报告写入成功后，任何审计或日志失败都不会删除报告。
- 自动完成检测只看真实文件。

### 日总结

- 总结包含 `C:\Users`、`\1`、正则和 Markdown 反斜杠时能原样写入。
- 模型调用期间日记新增记录时不写入过时总结。
- 空 content 只补答一次。

### JSON 与修订

- Planner 多行 query、字段缺失和 action/query 不一致时恰好修订一次。
- Reviewer 自身字段错误只重试 Reviewer。
- 一份正文的确定性失败和 Reviewer 拒绝共享一次内容修订。
- `length` 和不可解析的残缺 JSON 不进入内容修订。

### 周报

- 七天记录全部且只进入一个 Planner 组。
- 极长单日记录不会让 Planner 输入越过安全边界。
- 所有组 skip 时仍生成含诚实说明的周报。
- 所有查询无有效结果或全部 insufficient 时仍生成周报。
- 搜索网络/鉴权错误仍使周报失败并按类别重试。
- Researcher 修订不重复本次搜索。

### 月报

- 不调用 Planner、搜索和 Researcher。
- 只读取完整月内周报的回顾部分。
- Reviewer 能看到近期总结和周报回顾，并知道它们是派生上下文。
- 月报不复述周报探索段落。

### 阅读与来源

- 来源压缩不会修改正文、标题或网页 URL。
- 报告只显示日期或日记链接，不显示内部指纹。
- 报告显示输入、输出、缓存命中、缓存未命中和总 Token。

## 13. 最终判断

以 V4-Flash 当前能力、价格和官方输入缓存机制看，AgentRecord 不需要用本地数据库保存和复用语义正文来换取可靠性。恰恰相反，最可靠的兜底是：输入可重建、每次实时调用、错误明确分类、单项最多修订一次、强细节全部由程序完成、正式 Markdown 一次原子交付。

当前多 Agent 拆分本身基本合理，不需要增加角色。需要减少的是跨运行状态和无效协议：去掉 SQLite 生成依赖、让 JSON 只承载真正的控制决策、显式选择 thinking 模式，并把“没有可研究内容”视为有效语义结果。这样可以同时得到更小的代码、更清楚的失败边界和真正不受数据库内容影响的报告。
