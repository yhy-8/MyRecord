"""Plain-text contract for one retrospective section."""

import re

from .base import AgentPipelineError, AgentSpec


SPEC = AgentSpec(
    name="retrospective",
    purpose="整理周期事实、观点变化与行为模式",
    can_read_raw=True,
    instructions="""写一份可直接阅读的“整理与回顾”正文。
忠实回顾本周期做过什么、关注点如何分配，以及观点、理念、理想或行为模式出现了怎样的变化。行为分析只能是事实整理，不得把时间先后写成因果，不得心理诊断，不得给出行为教练式命令。
可以自然分段，但不要设计小节、列表或引用；中控会统一添加标题和本次输入对应的记录依据。
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
    if "```" in body or re.search(r"</?summary>", body, re.IGNORECASE):
        raise AgentPipelineError("Retrospective 不得输出包装标签或代码围栏")
    if re.search(r"(?m)^\s{0,3}#{1,6}\s+", body):
        raise AgentPipelineError("Retrospective 不得自行输出标题")
    if re.search(r"(?m)^\s*(?:[-*+]\s+|\d+[.)、]\s+)", body):
        raise AgentPipelineError("Retrospective 不得自行输出列表")
    if re.search(r"https?://", body, re.IGNORECASE):
        raise AgentPipelineError("Retrospective 不得自行输出 URL")
    return body
