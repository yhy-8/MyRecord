"""单次 Report Agent 与其调用协议（单模块）。

报告由**单次 Report Agent** 一次性直接读取中控提供的本周期完整原始记录流（按日期分隔 + 行号标注 +
全局引用编号 [N]）并生成完整报告正文。中控负责拼接记录流、写入头部审计元数据、原子交付；
**不做 AI 审核，也不做校验**（[N]↔记录映射校验与文末来源表为暂缓项）。
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
    return f"""[程序 Agent 任务:{spec.name}]
你是 MyRecord 的 {spec.name} Agent。{spec.purpose}。

【中控权限】
{permission_text}
你只负责当前这一项语义任务。任务拆分、引用编号、来源表、Markdown 结构、头部审计元数据和持久化
都由中控完成。在完整覆盖本任务所需信息的前提下保持简洁，避免重复、套话和无必要展开；不要为了
简短而省略重要内容。

【职责和输出约束】
{spec.instructions}

【本次任务】
{task}

【中控提供的输入】
{json.dumps(input_data, ensure_ascii=False)}"""


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
    instructions="""写一份可直接阅读、结构清晰的“周期报告”正文（Markdown）。
忠实回顾本周期做过什么、关注点如何分配，以及进展、问题、观点/想法变化等，全部以中控提供的
本周期完整原始记录流为唯一事实来源。

必须遵守：
- **引用规范**：凡由某条原始记录支撑的事实/结论，在正文对应位置用**简洁数字引用**标注，例如
  “完成了某项工作。[2]”。引用编号 [N] 直接使用中控输入里给出的序号。
- **不要生成文末来源表**：来源表、[N]↔记录映射、记录 ID（完整 `R-日期-行号` 形式）都由中控处理，
  你**只允许**在正文里写 [N]，不得输出任何以 `R-` 开头的来源标识或来源表。
- **不输出** JSON、代码围栏、包装标签。
- 正文允许使用 Markdown 标题、小节、要点列表；保持简洁、避免套话与无必要展开。
- 区分用户记录与引用的 AI 内容；禁止编造、心理诊断和行为教练式指示。""",
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