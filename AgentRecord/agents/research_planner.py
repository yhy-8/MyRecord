"""Select one privacy-safe research question for one record group."""

import re

from .base import AgentPipelineError, AgentSpec


SPEC = AgentSpec(
    name="research_planner",
    purpose="从一组周期记录中选择一个值得研究的公开领域问题",
    can_read_raw=True,
    instructions="""本次只判断中控给出的这一组记录。
如果存在值得通过公开资料研究、能够拓宽视野且不属于行为指导的问题，只返回一个简洁、清晰、适合直接搜索的公开问题。问题必须抽象化，不含姓名、联系方式、长数字、本地路径或可识别私人细节。
如果没有合适问题，action 使用 skip 且 query 为空；否则 action 使用 search 且 query 只包含一个问题。不要同时给出多个候选，不要补充标题、理由、编号或来源。
只返回 {"action":"search|skip","query":"一个问题或空字符串"}。""",
)


def _sanitize(text: str) -> str:
    value = re.sub(r"[\w.+-]+@[\w.-]+", "[email]", text)
    value = re.sub(r"(?<!\d)\d{7,}(?!\d)", "[number]", value)
    value = re.sub(
        r"(?:(?<!\w)[A-Za-z]:[\\/]|(?<![:/\w])/(?!/))[^\s]+",
        "[local-path]",
        value,
    )
    return re.sub(r"\s+", " ", value).strip()


def normalize_query(payload: dict) -> str | None:
    if set(payload) != {"action", "query"} or not all(
        isinstance(payload.get(key), str) for key in ("action", "query")
    ):
        raise AgentPipelineError(
            "ResearchPlanner 必须只返回字符串 action 和 query"
        )
    action = payload["action"].strip()
    raw_query = payload["query"].strip()
    if "\n" in raw_query or "\r" in raw_query or re.match(
        r"^(?:[-*•]|\d+[.)、])\s*", raw_query
    ):
        raise AgentPipelineError("ResearchPlanner query 必须是单个问题")
    query = raw_query
    query = _sanitize(query).strip(" `\"'“”")
    if action == "skip" and not query:
        return None
    if action != "search" or not query:
        raise AgentPipelineError("ResearchPlanner action 与 query 不一致")
    return query
