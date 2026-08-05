"""Shared minimal-JSON Agent invocation without persistence access."""

import json
import re
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class AgentSpec:
    name: str
    purpose: str
    can_read_raw: bool
    instructions: str


class AgentPipelineError(RuntimeError):
    """A validated multi-agent analysis run could not be completed."""

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


class AgentOutputError(AgentPipelineError):
    """The model call succeeded, but no complete minimal JSON object arrived."""


def _parse_json(text: str) -> dict:
    """Read one JSON object without extracting it from surrounding prose."""
    stripped = text.strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*\n?(.*?)\n?```",
        stripped,
        re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        stripped = fenced.group(1).strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError as error:
        raise AgentOutputError(
            f"Agent JSON 无法解析: {error}", response=text
        ) from error
    if not isinstance(value, dict):
        raise AgentOutputError("Agent JSON 顶层必须是对象", response=text)
    return value


def _prompt(
    spec: AgentSpec,
    task: str,
    input_data: dict,
    revision_context: dict | None = None,
) -> str:
    permission_text = (
        f"中控是否提供原始记录：{'是' if spec.can_read_raw else '否'}。"
        "只能使用本次输入，不能读写文件、数据库或调用工具。"
    )
    prompt = f"""[程序 Agent 任务:{spec.name}]
你是 AgentRecord 的 {spec.name} Agent。{spec.purpose}。

【中控权限】
{permission_text}
你只负责当前这一项语义任务。任务拆分、编号、来源绑定、搜索、审查调度、Markdown 结构和持久化都由中控完成。
在完整覆盖本任务所需信息的前提下保持简洁，避免重复、套话和无必要展开；不要为了简短而省略重要内容。

【职责和输出约束】
{spec.instructions}

【本次任务】
{task}

【中控提供的输入】
{json.dumps(input_data, ensure_ascii=False)}"""
    if revision_context:
        prompt += f"""

【中控修订请求】
这是同一项正文的一次有限修订。只根据反馈改写正文，不要解释修改过程。
{json.dumps(revision_context, ensure_ascii=False)}"""
    return prompt + """

只输出职责约束中指定的一个最小 JSON 对象，不要输出代码围栏、来源 ID、链接、完成提示或额外说明。JSON 中不得自行增加数组、嵌套对象或未指定字段。"""


def invoke_agent(
    spec: AgentSpec,
    task: str,
    input_data: dict,
    model_config: dict,
    call_model: Callable,
    *,
    revision_context: dict | None = None,
) -> tuple[dict, dict]:
    """Invoke one Agent for one minimal JSON object."""
    response = call_model(
        _prompt(spec, task, input_data, revision_context),
        model_config,
        structured_output=True,
    )
    text, success = response
    from ..ai_client import response_telemetry

    telemetry = response_telemetry(response)
    if not success:
        from ..ai_client import OUTPUT_FILTERED_MARKER, OUTPUT_TRUNCATED_MARKER

        if OUTPUT_TRUNCATED_MARKER in text or OUTPUT_FILTERED_MARKER in text:
            raise AgentOutputError(
                f"{spec.name} 输出未完整交付",
                response=text,
                telemetry=telemetry,
            )
        raise AgentPipelineError(
            f"{spec.name} 调用失败: {text}", response=text, telemetry=telemetry
        )
    try:
        return _parse_json(text), telemetry
    except AgentPipelineError as error:
        error.telemetry = telemetry
        raise
