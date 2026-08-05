# 各 Agent 功能与联动

中控位于 `analysis.orchestrator`。四个 Agent 使用同一 `current_model`，均无文件、数据库和工具权限。

## 1. 最小 JSON 契约

| Agent | 每次处理 | 输出 |
|---|---|---|
| Retrospective | 一组周期记录或一个有序分块 | `{"text":"连续正文"}` |
| ResearchPlanner | 一个中控记录组 | `{"action":"search|skip","query":"单个问题或空字符串"}` |
| Researcher | 一个问题及其搜索摘要 | `{"status":"supported|insufficient","text":"正文或不足原因"}` |
| Reviewer | 一份回顾或一个研究主题 | `{"approved":true或false,"feedback":"一段意见或空字符串"}` |

输出只能包含约定字段和标量类型，不得增加数组、嵌套对象或额外字段。Retrospective/Researcher 正文和 Planner 查询还不得包含模型自拟标题、列表、来源 ID 或 URL。

日记顶部总结不是正式 Agent 流程，只有一份 Markdown 正文，因此直接使用文本响应。

## 2. 回顾链路

Retrospective 读取本周期记录、显式引用的日记记录、近期总结，以及月报可用的同期周报。它负责忠实整理事实与变化，不得把时间先后写成因果，不得心理诊断或给出行为命令。

中控把该次调用的全部记录绑定为依据并生成 Markdown。输入过大时，中控先按原文顺序分块，每块单独生成和审查，再按顺序合并。

Reviewer 核对事实、时期、身份、来源覆盖、因果越界和行为教练倾向。字段或正文校验失败、或者 Reviewer 拒绝时，原 Agent 共享最多一次修订机会。无法解析的 JSON 和 API 错误直接结束当前阶段。

## 3. 周报研究链路

1. 回顾通过后，中控按日期顺序把全部本周记录分成最多五个连续组。
2. 每组调用一次 ResearchPlanner，只决定一个公开查询或跳过。
3. 中控分配 `Q-*`，逐字执行每个查询，过滤 URL 并分配 `W-*`。
4. 每个有搜索证据的主题调用一次 Researcher；Researcher 看不到 URL 或来源 ID。
5. 中控把该组全部 `R-*` 和该主题全部有效 `W-*` 绑定到正文并渲染标题与链接。
6. Reviewer 分别审查每个主题；仍不合格的主题独立丢弃，至少一个主题通过即可交付。

Planner 跳过的分组不会产生研究主题，但该组记录已经被明确检查，不会因为中控只选“记录较多的日期”而遗漏。

## 4. 月报链路

月报只运行：

```text
Retrospective → Reviewer → 中控组装
```

它不运行 Planner、搜索或 Researcher。

## 5. 交付

中控统一生成报告标题、板块标题、来源索引、模型、耗时和 Token 用量。报告先写入唯一临时文件，再原子替换目标文件；只有文件和运行状态都成功时才视为完成。
