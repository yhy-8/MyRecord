"""Plain-text contract for one retrospective section."""

import re

from .base import AgentPipelineError, AgentSpec, is_json_container


SPEC = AgentSpec(
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


def validate(text: str) -> str:
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
