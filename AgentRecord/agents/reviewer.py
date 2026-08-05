"""Independent quality gate for the two report sections."""

from .base import AgentPipelineError, AgentSpec


SPEC = AgentSpec(
    name="reviewer",
    purpose="分别审查整理回顾和领域研究",
    can_read_raw=True,
    readable_node_types=frozenset(),
    writable_node_types=frozenset(),
    writable_relation_types=frozenset(),
    allowed_tools=frozenset(),
    instructions="""严格但只按实质问题检查事实、时期、身份、来源覆盖、因果越界、心理诊断、套话和行为教练倾向，不因措辞偏好或可选润色否决板块。
retrospective_review 模式必须把正文与 review_context 中的最小记录集合逐项对照，不能因为存在 [R-*] 格式就假定来源支持判断；topic_decisions 返回空数组。
research_review 模式逐项检查每个 research_topics：外部来源是否真正支持正文，是否包含反例或边界，是否把探索性推断明确标为推断，以及是否避免替用户做最终判断。对每个输入 topic_id 返回 accepted 或 rejected；证据薄弱或正文越界时拒绝该主题，不要为了整份报告凑齐主题。
pass 只表示板块正文是否可以按当前稿交付。pass=false 时 required_changes 或 unsupported_claims 必须给出能直接修改的具体意见；pass=true 时两者必须为空。
研究审查的 pass 只有在全部主题都 accepted 时为 true；部分主题 rejected 时为 false，但中控仍可交付其余 accepted 主题。
只返回 JSON：{"pass":true或false,"topic_decisions":[{"topic_id":"Q001","status":"accepted|rejected","reason":"..."}],"unsupported_claims":["..."],"required_changes":["..."],"summary":"..."}。""",
)


def validate(
    payload: dict,
    *,
    expected_topic_ids: set[str] | None = None,
) -> tuple[bool, dict[str, str], list[str]]:
    if not isinstance(payload.get("pass"), bool):
        raise AgentPipelineError("Reviewer 缺少布尔 pass")
    required = payload.get("required_changes", [])
    unsupported = payload.get("unsupported_claims", [])
    topic_decisions = payload.get("topic_decisions", [])
    if not isinstance(required, list) or not isinstance(unsupported, list):
        raise AgentPipelineError("Reviewer 修改意见格式错误")
    if not isinstance(topic_decisions, list):
        raise AgentPipelineError("Reviewer topic_decisions 必须是数组")
    expected_topics = expected_topic_ids or set()
    normalized_topics: dict[str, str] = {}
    topic_reasons = []
    for decision in topic_decisions:
        if not isinstance(decision, dict):
            raise AgentPipelineError("Reviewer 主题决定必须是对象")
        topic_id = str(decision.get("topic_id", "")).strip()
        status = str(decision.get("status", "")).strip()
        reason = str(decision.get("reason", "")).strip()
        if topic_id not in expected_topics or topic_id in normalized_topics:
            raise AgentPipelineError("Reviewer 引用未知或重复研究主题")
        if status not in {"accepted", "rejected"} or not reason:
            raise AgentPipelineError("Reviewer 主题决定缺少有效状态或原因")
        normalized_topics[topic_id] = status
        if status == "rejected":
            topic_reasons.append(f"{topic_id}: {reason}")
    if set(normalized_topics) != expected_topics:
        raise AgentPipelineError("Reviewer 未审查全部研究主题")

    feedback = [
        str(item)
        for item in [*required, *unsupported, *topic_reasons]
        if str(item).strip()
    ]
    if expected_topics and payload["pass"] != all(
        status == "accepted" for status in normalized_topics.values()
    ):
        raise AgentPipelineError("Reviewer research pass 与逐主题决定不一致")
    if payload["pass"] and feedback:
        raise AgentPipelineError("Reviewer 通过时不应同时要求修改")
    if not payload["pass"] and not feedback:
        raise AgentPipelineError("Reviewer 否决时必须给出具体修改意见")
    return payload["pass"], normalized_topics, feedback
