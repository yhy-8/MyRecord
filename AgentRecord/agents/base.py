"""Shared Agent contract and model invocation without persistence access."""

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
    """The model call succeeded, but its structured output could not be read."""


def _parse_json(text: str) -> dict:
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
        # Some OpenAI-compatible endpoints occasionally append a lone quote or
        # closing Markdown fence after an otherwise complete JSON object.  This
        # is unambiguous to recover, unlike extracting JSON from explanatory
        # prose or attempting to repair malformed content.
        if error.msg == "Extra data":
            try:
                value, end = json.JSONDecoder().raw_decode(stripped)
            except json.JSONDecodeError:
                value, end = None, 0
            trailing = stripped[end:].strip()
            if not (
                isinstance(value, dict)
                and re.fullmatch(r"(?:[`'\"}\]]|\s)*", trailing)
            ):
                raise AgentOutputError(
                    f"Agent JSON 无法解析: {error}", response=text
                ) from error
        else:
            raise AgentOutputError(
                f"Agent JSON 无法解析: {error}", response=text
            ) from error
    if not isinstance(value, dict):
        raise AgentOutputError("Agent JSON 顶层必须是对象", response=text)
    return value


def cited_source_ids(markdown: str) -> set[str]:
    """Return source IDs appearing inside Markdown citation brackets."""
    refs: set[str] = set()
    for citation in re.findall(r"\[([^\]\n]+)\]", markdown):
        refs.update(
            re.findall(r"R-\d{8}-\d{3}(?:-[0-9a-f]{12})?", citation)
        )
        for match in re.finditer(
            r"R-(\d{8})-(\d{3})\s*(?:~|～|–|—|至)\s*"
            r"(?:(?:R-(\d{8})-)?(\d{3}))",
            citation,
        ):
            start_date, start_text, end_date, end_text = match.groups()
            if end_date and end_date != start_date:
                continue
            start_number = int(start_text)
            end_number = int(end_text)
            # A range is only shorthand within one diary.  Bound expansion so
            # malformed model output cannot create an enormous review context.
            if start_number <= end_number and end_number - start_number <= 200:
                refs.update(
                    f"R-{start_date}-{number:03d}"
                    for number in range(start_number, end_number + 1)
                )
    return refs


def _prompt(
    spec: AgentSpec,
    task: str,
    input_data: dict,
    revision_context: dict | None = None,
) -> str:
    permission_text = (
        f"中控是否提供原始记录：{'是' if spec.can_read_raw else '否'}。"
        "只能使用本次输入 JSON，不能读写文件、数据库或调用工具。"
    )
    prompt = f"""[程序 Agent 任务:{spec.name}]
你是 AgentRecord 的 {spec.name} Agent。{spec.purpose}。

【中控权限】
{permission_text}
你只返回候选 JSON；中控负责搜索、数据库和文件写入。

【职责和输出契约】
{spec.instructions}

【本次任务】
{task}

【中控提供的输入 JSON】
{json.dumps(input_data, ensure_ascii=False)}"""
    if revision_context:
        prompt += f"""

【中控修订请求】
这是同一阶段的有限修订，不是新任务。保留原稿中正确且有依据的内容，只修正下列问题，然后重新输出完整结果；不要解释修改过程。
{json.dumps(revision_context, ensure_ascii=False)}"""
    return prompt + """

只输出一个符合契约的 JSON 对象，不要输出代码围栏、解释或完成提示。"""


def invoke_agent(
    spec: AgentSpec,
    task: str,
    input_data: dict,
    model_config: dict,
    call_model: Callable,
    *,
    revision_context: dict | None = None,
) -> dict:
    """Invoke one Agent with controller-supplied input and no tool access."""
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
        payload = _parse_json(text)
    except AgentPipelineError as error:
        error.telemetry = telemetry
        raise
    payload["_telemetry"] = telemetry
    return payload
