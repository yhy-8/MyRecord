"""Select privacy-safe research topics from period records."""

import re

from .base import AgentPipelineError, AgentSpec


SPEC = AgentSpec(
    name="research_planner",
    purpose="从周期记录中选择少量值得研究的公开领域问题",
    can_read_raw=True,
    readable_node_types=frozenset(),
    writable_node_types=frozenset(),
    writable_relation_types=frozenset(),
    allowed_tools=frozenset(),
    instructions="""为周报第二板块自主选择一至五个研究主题，主题数量由实际材料和研究价值决定，不要为了凑数拆分或补写。主题来自记录中的观点、问题或兴趣；目标是拓宽视野，而不是给用户下行为指令。
只选择能够通过公开资料实质研究的领域问题。查询必须抽象化，不包含姓名、联系方式、长数字、本地路径或可识别私人细节。source_refs 必须引用促成该主题的记录，origin 固定为 records。
不要生成 topic_id，中控会按最终顺序分配稳定 ID。
只返回 JSON：{"topics":[{"title":"...","query":"适合公开搜索的查询","reason":"为何值得研究","origin":"records","source_refs":["R-..."]}]}。""",
)


def _sanitize(text: str, limit: int) -> str:
    value = re.sub(r"[\w.+-]+@[\w.-]+", "[email]", text)
    value = re.sub(r"(?<!\d)\d{7,}(?!\d)", "[number]", value)
    value = re.sub(
        r"(?:(?<!\w)[A-Za-z]:[\\/]|(?<![:/\w])/(?!/))[^\s]+",
        "[local-path]",
        value,
    )
    return value.strip()[:limit]


def validate(payload: dict, allowed_source_ids: set[str]) -> list[dict]:
    raw_topics = payload.get("topics", [])
    if not isinstance(raw_topics, list) or not 1 <= len(raw_topics) <= 5:
        raise AgentPipelineError("ResearchPlanner 必须返回一至五个主题")
    topics = []
    seen_titles = set()
    for index, raw in enumerate(raw_topics, 1):
        if not isinstance(raw, dict):
            raise AgentPipelineError("研究主题必须是对象")
        topic_id = f"Q{index:03d}"
        title = re.sub(r"\s+", " ", _sanitize(str(raw.get("title", "")), 200))
        query = _sanitize(str(raw.get("query", "")), 240)
        reason = _sanitize(str(raw.get("reason", "")), 500)
        origin = str(raw.get("origin", "")).strip()
        refs = raw.get("source_refs", [])
        if not title or not query or not reason:
            raise AgentPipelineError("研究主题缺少标题、查询或理由")
        title_key = title.casefold()
        if title_key in seen_titles:
            raise AgentPipelineError("研究主题标题必须唯一")
        if origin != "records":
            raise AgentPipelineError("研究主题必须由周期记录驱动")
        if not isinstance(refs, list) or any(
            not isinstance(ref, str) or ref not in allowed_source_ids for ref in refs
        ):
            raise AgentPipelineError("研究主题包含未知记录来源")
        if not refs:
            raise AgentPipelineError("记录驱动研究主题必须引用记录")
        topics.append(
            {
                "topic_id": topic_id,
                "title": title,
                "query": query,
                "reason": reason,
                "origin": origin,
                "source_refs": list(dict.fromkeys(refs)),
            }
        )
        seen_titles.add(title_key)
    return topics
