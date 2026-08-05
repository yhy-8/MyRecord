"""Minimal JSON quality gate for one report section or research topic."""

from .base import AgentPipelineError, AgentSpec


SPEC = AgentSpec(
    name="reviewer",
    purpose="审查一份回顾正文或一个研究主题正文",
    can_read_raw=True,
    instructions="""本次只审查中控给出的一份正文。
核对事实、时期、身份、来源覆盖、因果越界、心理诊断、套话和行为教练倾向。研究正文还要检查外部资料是否支持主要判断、是否说明边界和不确定性，以及是否避免替用户做最终判断。只报告影响真实性、可追溯性或交付质量的实质问题，不因措辞偏好否决。
正文可以直接交付时 approved=true 且 feedback 为空；不能交付时 approved=false，并在 feedback 中只写一段具体、可执行的修改意见。
只返回 {"approved":true或false,"feedback":""}。""",
)


def validate(payload: dict) -> tuple[bool, str]:
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
