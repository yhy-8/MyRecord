"""单次 Report Agent 与其调用协议（单模块）。

报告由**单次 Report Agent** 一次性直接读取中控提供的本周期完整原始记录流（按天分块 + 块内行号标注）
并生成完整周期报告。输入按天分块：块首 `[YYYYMMDD]`，块内每行 `行号: 内容`。

Agent 返回**纯 JSON 对象**：`summary`（含 `[n]` 引用编号的报告正文）与 `references` 数组（每个编号
对应一个 `R-YYYYMMDD-行号` / `R-YYYYMMDD-起始行-结束行` 来源）。中控负责拼接输入、解析 JSON、
校验引用合法性、写入头部审计元数据与文末来源表、原子交付；不做 AI 审核。
"""

import json
import re
from dataclasses import dataclass
from typing import Callable


def is_json_container(text: str) -> bool:
    """返回整个文本是否为 JSON 对象或数组。"""
    try:
        value = json.loads(text.strip())
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(value, (dict, list))


@dataclass(frozen=True)
class AgentSpec:
    name: str
    purpose: str
    can_read_raw: bool
    instructions: str
    structured_output: bool
    thinking: bool
    max_tokens: int


class AgentPipelineError(RuntimeError):
    """一次受校验的 Agent 运行未能完成。"""

    def __init__(
        self,
        message: str,
        *,
        response: str = "",
        telemetry: dict | None = None,
    ):
        super().__init__(message)
        self.response = response
        self.telemetry = telemetry or {}


def _prompt(spec: AgentSpec, task: str, input_data: dict) -> str:
    permission_text = (
        f"中控是否提供原始记录：{'是' if spec.can_read_raw else '否'}。"
        "只能使用本次输入，不能读写文件、数据库或调用工具。"
    )
    json_schema = (
        "{\n"
        '  "summary": "包含引用编号的总结文本",\n'
        '  "references": [\n'
        '    {"id": 1, "source": "R-20260816-1"},\n'
        '    {"id": 2, "source": "R-20260816-2-3"}\n'
        "  ]\n"
        "}"
    )
    example = (
        "示例输入：\n"
        "[20260816]\n"
        "1: 今天去了公园，天气很好。\n"
        "2: 下午和朋友聊天，讨论了项目计划。\n"
        "3: 晚上读了一本书，很有启发。\n\n"
        "示例输出：\n"
        "{\n"
        '  "summary": "本周进行了户外活动[1]，并与朋友讨论项目计划、读书，收获颇多。[2]",\n'
        '  "references": [\n'
        '    {"id": 1, "source": "R-20260816-1"},\n'
        '    {"id": 2, "source": "R-20260816-2-3"}\n'
        "  ]\n"
        "}"
    )
    return f"""[程序 Agent 任务:{spec.name}]
你是 MyRecord 的 {spec.name} Agent。{spec.purpose}。

【中控权限】
{permission_text}
你只负责当前这一项语义任务。任务拆分、引用编号、来源表、Markdown 结构、头部审计元数据和持久化
都由中控完成。在完整覆盖本任务所需信息的前提下保持简洁，避免重复、套话和无必要展开；不要为了
简短而省略重要内容。

【职责、引用规则与输出约束】
{spec.instructions}

【JSON 输出格式】
必须返回一个纯 JSON 对象，不要任何额外文字或 Markdown 代码围栏。结构如下：
{json_schema}

【示例】
{example}

【本次任务】
{task}

【中控提供的原始记录（按天分块：块首 [YYYYMMDD]，块内每行 行号: 内容）】
{input_data}"""


def invoke_agent(
    spec: AgentSpec,
    task: str,
    input_data: dict,
    model_config: dict,
    call_model: Callable,
) -> tuple[str, dict]:
    """调用一次 Report Agent（纯文本单次交付）。"""
    from ..ai_client import response_telemetry

    prompt = _prompt(spec, task, input_data)
    response = call_model(
        prompt,
        model_config,
        structured_output=spec.structured_output,
        thinking=spec.thinking,
        max_tokens=spec.max_tokens,
    )
    text, success = response
    telemetry = response_telemetry(response)
    if not success:
        from ..ai_client import OUTPUT_FILTERED_MARKER, OUTPUT_TRUNCATED_MARKER

        if OUTPUT_TRUNCATED_MARKER in text or OUTPUT_FILTERED_MARKER in text:
            raise AgentPipelineError(
                f"{spec.name} 输出未完整交付",
                response=text,
                telemetry=telemetry,
            )
        raise AgentPipelineError(
            f"{spec.name} 调用失败: {text}", response=text, telemetry=telemetry
        )
    return text.strip(), telemetry


# ---------- Agent 定义 ----------


REPORT_SPEC = AgentSpec(
    name="report",
    purpose="一次性整理本周期原始记录并生成完整周期报告",
    can_read_raw=True,
    instructions="""在 **summary** 字段中写一份可直接阅读、结构清晰的“周期报告”正文（Markdown，可用标题、
小节、要点列表）。忠实回顾本周期做过什么、关注点如何分配，以及进展、问题、观点/想法变化等，
全部以中控提供的本周期完整原始记录流为唯一事实来源。

引用规则（所有标点为英文半角）：
- 总结正文引用具体信息时，在对应句子末尾标注引用编号，例如“完成了某项工作。[1]”。编号从 1 开始。
- 在 **references** 数组中列出每个编号对应的来源，每项 {"id": 编号, "source": "R-日期-行号"}。
- 来源格式：
  - 单行：`R-YYYYMMDD-行号`，例如 `R-20260816-1`。
  - 多行连续：`R-YYYYMMDD-起始行-结束行`，例如 `R-20260816-2-3`。
- **只引用输入中实际出现的行号，不得编造**：若信息来自连续多行则合并为一个引用，单行则只写一个行号。
- references 按 id 升序排列。

输出约束：
- 只输出一个**纯 JSON 对象**，不要任何额外文字、说明、Markdown 代码围栏或包装标签。
- summary 内文可用 Markdown 排版；禁止编造、心理诊断和行为教练式指示；区分用户记录与引用的 AI 内容。""",
    structured_output=False,
    thinking=True,
    max_tokens=65536,
)


AGENTS = {spec.name: spec for spec in (REPORT_SPEC,)}


__all__ = [
    "AGENTS",
    "AgentPipelineError",
    "AgentSpec",
    "REPORT_SPEC",
    "invoke_agent",
    "is_json_container",
]