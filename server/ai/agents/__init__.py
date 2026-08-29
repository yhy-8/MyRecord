"""独立分析 Agent 与其共用的调用协议（单模块）。

两种语义职责：retrospective（整理与回顾，纯文本正文）与 reviewer（审查，
最小 JSON 对象）。共享同一套 AgentSpec / invoke_agent 协议，无持久化访问。
"""

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
    """一次受校验的多 Agent 分析运行未能完成。"""

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
    """模型调用成功，但未能得到完整的最小 JSON 对象。"""


def is_json_container(text: str) -> bool:
    """返回整个文本是否为 JSON 对象或数组。"""
    try:
        value = json.loads(text.strip())
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(value, (dict, list))


def _parse_json(text: str) -> dict:
    """读取一个 JSON 对象，不从周围散文里抽段。"""
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
你是 MyRecord 的 {spec.name} Agent。{spec.purpose}。

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

只输出职责约束中指定的连续正文，不要输出 JSON、代码围栏、来源 ID、链接、标题、完成提示或额外说明。"""


def _merge_telemetry(items: list[dict]) -> dict:
    """合并报告元数据用到的少量遥测字段。"""
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
    """调用一个 Agent，适用时带一次有界的 JSON 协议重试。"""
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


# ---------- Agent 定义 ----------


RETROSPECTIVE_SPEC = AgentSpec(
    name="retrospective",
    purpose="整理周期事实、观点变化与行为模式",
    can_read_raw=True,
    instructions="""写一份可直接阅读的“整理与回顾”正文。
忠实回顾本周期做过什么、关注点如何分配，以及观点、理念、理想或行为模式出现了怎样的变化。行为分析只能是事实整理，不得把时间先后写成因果，不得心理诊断，不得给出行为教练式命令。
用**短要点分点**呈现（Markdown 无序列表），每条一个独立要点，避免写成一大段连续文字，让排版清晰、阅读舒服；可用加粗主题词作为要点开头引导（例如 **工作进展**、**遇到的问题**、**想法变化**），按本周期实际情况灵活分组。不要自行输出标题或编号小节；中控会统一添加标题和本次输入对应的记录依据。
只返回完整正文。""",
    structured_output=False,
    thinking=True,
    max_tokens=65536,
)

REVIEWER_SPEC = AgentSpec(
    name="reviewer",
    purpose="审查一份整理与回顾正文",
    can_read_raw=True,
    instructions="""本次只审查中控给出的一份“整理与回顾”正文。
核对事实、时期、身份、来源覆盖、因果越界、心理诊断、套话和行为教练倾向。facts 是事实材料；context 是派生辅助上下文，不能单独证明当期事实。要点式排版属于允许的交付形式；只报告影响真实性、可追溯性或交付质量的实质问题，不因措辞偏好否决。
正文可以直接交付时 approved=true 且 feedback 为空；不能交付时 approved=false，并在 feedback 中只写一段具体、可执行的修改意见。
只返回 {"approved":true或false,"feedback":""}。""",
    structured_output=True,
    thinking=True,
    max_tokens=16384,
)


AGENTS = {
    spec.name: spec for spec in (RETROSPECTIVE_SPEC, REVIEWER_SPEC)
}


def validate_retrospective(text: str) -> str:
    """校验整理与回顾正文，返回清洗后的正文。"""
    if not isinstance(text, str):
        raise AgentPipelineError("Retrospective 必须返回纯文本正文")
    body = text.strip()
    if not body:
        raise AgentPipelineError("Retrospective 正文为空")
    if is_json_container(body):
        raise AgentPipelineError("Retrospective 不得输出 JSON")
    if "```" in body or re.search(r"</?summary>", body, re.IGNORECASE):
        raise AgentPipelineError("Retrospective 不得输出包装标签或代码围栏")
    if re.search(r"(?m)^\s{0,3}#{1,6}\s+", body):
        raise AgentPipelineError("Retrospective 不得自行输出标题")
    if re.search(r"https?://", body, re.IGNORECASE):
        raise AgentPipelineError("Retrospective 不得自行输出 URL")
    return body


def validate_reviewer(payload: dict) -> tuple[bool, str]:
    """校验审查者的最小 JSON 结论，返回 (approved, feedback)。"""
    if set(payload) != {"approved", "feedback"}:
        raise AgentPipelineError("Reviewer 必须只返回 approved 和 feedback")
    approved = payload.get("approved")
    feedback_value = payload.get("feedback")
    if not isinstance(approved, bool) or not isinstance(feedback_value, str):
        raise AgentPipelineError(
            "Reviewer approved 必须是布尔值且 feedback 必须是字符串"
        )
    feedback = feedback_value.strip()
    if approved and not feedback:
        return True, ""
    if not approved and feedback:
        return False, feedback
    raise AgentPipelineError("Reviewer approved 与 feedback 不一致")


__all__ = [
    "AGENTS",
    "AgentOutputError",
    "AgentPipelineError",
    "AgentSpec",
    "RETROSPECTIVE_SPEC",
    "REVIEWER_SPEC",
    "invoke_agent",
    "is_json_container",
    "validate_reviewer",
    "validate_retrospective",
]