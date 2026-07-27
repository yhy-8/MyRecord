"""Structured semantic contract for the report's retrospective section."""

import re

from .base import AgentPipelineError, AgentSpec, confidence


PROFILE_CATEGORIES = {
    "viewpoint",
    "principle",
    "ideal",
    "behavior_pattern",
    "interest",
}


SPEC = AgentSpec(
    name="retrospective",
    purpose="整理周期事实，并更新可追溯的人物观点与行为画像",
    can_read_raw=True,
    readable_node_types=frozenset(PROFILE_CATEGORIES),
    writable_node_types=frozenset(PROFILE_CATEGORIES),
    writable_relation_types=frozenset(),
    allowed_tools=frozenset(),
    instructions="""生成报告第一板块“整理与回顾”的语义内容，并提出少量值得长期保存的人物画像更新。
内容必须忠实回顾本周期做过什么、关注点如何分配，以及观点、理念、理想或行为模式出现了怎样的变化。行为分析属于事实整理的一部分，但不得把时间先后写成因果，不得心理诊断，不得给出行为教练式命令。
不要生成 Markdown 标题、引用标点或来源索引。把正文拆成少量 section，每个 paragraph 只填写自然语言 text，并在 source_refs 数组中原样选择直接支持该段的输入来源 ID；中控负责标题、[R-*] 引用和排版。每段必须有来源，不能自行编造、缩写或扩展来源 ID。
历史画像只用于比较此前状态；不得使用晚于报告周期结束的内容。新的画像条目只保存相对稳定或反复出现的内容，不保存一次性事件、任务、外部事实或 AI 自己的建议。supersedes_id 只能复制输入中的 P 三位短别名；没有明确变化时为 null。
不要生成 temp_id，中控会为候选分配临时 ID。
只返回 JSON：{"sections":[{"title":"小节标题，可为空","paragraphs":[{"text":"一个自然语言段落","source_refs":["R-..."]}]}],"profile_entries":[{"category":"viewpoint|principle|ideal|behavior_pattern|interest","title":"...","statement":"...","confidence":0到1,"source_refs":["R-..."],"supersedes_id":null}]}。""",
)


def _plain_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _validated_sections(
    payload: dict, allowed_source_ids: set[str]
) -> tuple[list[dict], str]:
    raw_sections = payload.get("sections", [])
    if not isinstance(raw_sections, list) or not 1 <= len(raw_sections) <= 8:
        raise AgentPipelineError("Retrospective sections 必须是一至八项数组")
    sections = []
    total_length = 0
    for raw_section in raw_sections:
        if not isinstance(raw_section, dict):
            raise AgentPipelineError("Retrospective section 必须是对象")
        title = _plain_text(raw_section.get("title"))
        if len(title) > 200:
            raise AgentPipelineError("Retrospective 小节标题过长")
        raw_paragraphs = raw_section.get("paragraphs", [])
        if not isinstance(raw_paragraphs, list) or not 1 <= len(raw_paragraphs) <= 8:
            raise AgentPipelineError("Retrospective paragraphs 必须是一至八项数组")
        paragraphs = []
        for raw_paragraph in raw_paragraphs:
            if not isinstance(raw_paragraph, dict):
                raise AgentPipelineError("Retrospective paragraph 必须是对象")
            text = _plain_text(raw_paragraph.get("text"))
            refs = raw_paragraph.get("source_refs", [])
            if not text:
                raise AgentPipelineError("Retrospective paragraph text 为空")
            if len(text) > 4000:
                raise AgentPipelineError("Retrospective 单段超过 4000 字符")
            if not isinstance(refs, list) or not refs:
                raise AgentPipelineError("Retrospective 每段必须选择 source_refs")
            if any(
                not isinstance(ref, str) or ref not in allowed_source_ids
                for ref in refs
            ):
                raise AgentPipelineError("Retrospective paragraph 包含未知来源")
            normalized_refs = list(dict.fromkeys(refs))
            paragraphs.append({"text": text, "source_refs": normalized_refs})
            total_length += len(text)
        sections.append({"title": title, "paragraphs": paragraphs})
    if total_length > 24000:
        raise AgentPipelineError("Retrospective 正文超过 24000 字符")

    rendered = []
    for section in sections:
        if section["title"]:
            rendered.append(f"### {section['title']}")
        for paragraph in section["paragraphs"]:
            citations = ", ".join(paragraph["source_refs"])
            rendered.append(f"{paragraph['text']} [{citations}]")
    return sections, "\n\n".join(rendered)


def validate(
    payload: dict,
    *,
    allowed_source_ids: set[str],
    current_source_ids: set[str],
    visible_profile_ids: set[str],
    visible_profiles: dict[str, dict] | None = None,
) -> tuple[str, list[dict]]:
    _, markdown = _validated_sections(payload, allowed_source_ids)
    raw_entries = payload.get("profile_entries", [])
    if not isinstance(raw_entries, list) or len(raw_entries) > 12:
        raise AgentPipelineError("Retrospective profile_entries 必须是不超过 12 项的数组")
    entries = []
    superseded = set()
    seen_signatures = set()
    existing_signatures = {
        (
            str(profile.get("category", "")).strip(),
            re.sub(r"\s+", "", str(profile.get("title", ""))).casefold(),
            re.sub(r"\s+", "", str(profile.get("statement", ""))).casefold(),
        ): profile_id
        for profile_id, profile in (visible_profiles or {}).items()
    }
    for index, raw in enumerate(raw_entries, 1):
        if not isinstance(raw, dict):
            raise AgentPipelineError("人物画像条目必须是对象")
        temp_id = f"p{index}"
        category = str(raw.get("category", "")).strip()
        title = str(raw.get("title", "")).strip()
        statement = str(raw.get("statement", "")).strip()
        refs = raw.get("source_refs", [])
        supersedes_id = raw.get("supersedes_id") or None
        if category not in PROFILE_CATEGORIES:
            raise AgentPipelineError("人物画像 category 无效")
        if not title or not statement:
            raise AgentPipelineError("人物画像缺少标题或陈述")
        if len(title) > 200 or len(statement) > 2000:
            raise AgentPipelineError("人物画像标题或陈述过长")
        if not isinstance(refs, list) or any(
            not isinstance(ref, str) or ref not in allowed_source_ids for ref in refs
        ):
            raise AgentPipelineError("人物画像包含未知来源")
        if not set(refs) & current_source_ids:
            raise AgentPipelineError("人物画像更新必须有本周期来源")
        if category == "behavior_pattern" and len(set(refs)) < 2:
            raise AgentPipelineError("行为模式必须由至少两条不同记录共同支持")
        if supersedes_id and supersedes_id not in visible_profile_ids:
            raise AgentPipelineError("人物画像尝试替代不可见条目")
        if supersedes_id and supersedes_id in superseded:
            raise AgentPipelineError("一次报告不能用多个候选替代同一人物画像")
        signature = (
            category,
            re.sub(r"\s+", "", title).casefold(),
            re.sub(r"\s+", "", statement).casefold(),
        )
        if signature in seen_signatures:
            raise AgentPipelineError("一次报告不能创建重复的人物画像候选")
        existing_id = existing_signatures.get(signature)
        if existing_id and supersedes_id != existing_id:
            raise AgentPipelineError("人物画像与现有条目重复，必须明确替代原条目")
        entries.append(
            {
                "temp_id": temp_id,
                "category": category,
                "title": title,
                "statement": statement,
                "confidence": confidence(raw.get("confidence", 0.5), "confidence"),
                "source_refs": list(dict.fromkeys(refs)),
                "supersedes_id": supersedes_id,
            }
        )
        seen_signatures.add(signature)
        if supersedes_id:
            superseded.add(supersedes_id)
    return markdown, entries
