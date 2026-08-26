"""Shared single-task Agent invocation without persistence access."""

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
    structured_output: bool
    thinking: bool
    max_tokens: int


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


def is_json_container(text: str) -> bool:
    """Return whether the entire text is a JSON object or array."""
    try:
        value = json.loads(text.strip())
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(value, (dict, list))


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
这是同一项正文的本次有限修订。只根据反馈改写正文，不要解释修改过程。
{json.dumps(revision_context, ensure_ascii=False)}"""
    if spec.structured_output:
        return prompt + """

只输出职责约束中指定的一个最小 JSON 对象，不要输出代码围栏、来源 ID、链接、完成提示或额外说明。JSON 中不得自行增加数组、嵌套对象或未指定字段。"""
    return prompt + """

只输出职责约束中指定的连续正文，不要输出 JSON、代码围栏、来源 ID、链接、标题、列表、完成提示或额外说明。"""


def _merge_telemetry(items: list[dict]) -> dict:
    """Merge the small subset of telemetry used by report metadata."""
    merged = {
        "duration_ms": 0,
        "http_attempts": 0,
        "finish_reasons": [],
        "empty_content_retries": 0,
        "protocol_retries": max(0, len(items) - 1),
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "cache_miss_tokens": 0,
        },
    }
    for item in items:
        merged["duration_ms"] += int(item.get("duration_ms", 0) or 0)
        merged["http_attempts"] += int(item.get("http_attempts", 0) or 0)
        merged["empty_content_retries"] += int(
            item.get("empty_content_retries", 0) or 0
        )
        merged["finish_reasons"].extend(item.get("finish_reasons", []))
        usage = item.get("usage", {})
        for key in merged["usage"]:
            merged["usage"][key] += int(usage.get(key, 0) or 0)
    return merged


def invoke_agent(
    spec: AgentSpec,
    task: str,
    input_data: dict,
    model_config: dict,
    call_model: Callable,
    *,
    revision_context: dict | None = None,
) -> tuple[dict | str, dict]:
    """Invoke one Agent, with one bounded JSON protocol retry when applicable."""
    from ..ai_client import response_telemetry

    base_prompt = _prompt(spec, task, input_data, revision_context)
    prompt = base_prompt
    telemetry_items = []
    protocol_attempts = 2 if spec.structured_output else 1
    for attempt in range(protocol_attempts):
        response = call_model(
            prompt,
            model_config,
            structured_output=spec.structured_output,
            thinking=spec.thinking,
            max_tokens=spec.max_tokens,
        )
        text, success = response
        telemetry_items.append(response_telemetry(response))
        telemetry = _merge_telemetry(telemetry_items)
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
        if not spec.structured_output:
            return text.strip(), telemetry
        try:
            return _parse_json(text), telemetry
        except AgentOutputError as error:
            if attempt + 1 == protocol_attempts:
                error.telemetry = telemetry
                raise
            prompt = (
                base_prompt
                + "\n\n【协议重试】上一响应不是可解析的目标 JSON 对象。"
                "请重新执行原任务，并严格只返回指定 JSON。"
            )
    raise RuntimeError("unreachable")
