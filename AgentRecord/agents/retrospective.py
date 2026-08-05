"""Structured semantic contract for the report's retrospective section."""

import re

from .base import AgentPipelineError, AgentSpec


SPEC = AgentSpec(
    name="retrospective",
    purpose="整理周期事实、观点变化与行为模式",
    can_read_raw=True,
    instructions="""生成报告第一板块“整理与回顾”的语义内容。
内容必须忠实回顾本周期做过什么、关注点如何分配，以及观点、理念、理想或行为模式出现了怎样的变化。行为分析属于事实整理的一部分，但不得把时间先后写成因果，不得心理诊断，不得给出行为教练式命令。
不要生成 Markdown 标题、引用标点或来源索引。把正文放在一个扁平的 paragraphs 数组中；每项只填写可选小节标题 title、自然语言 text，并在 source_refs 数组中原样选择直接支持该段的输入来源 ID。paragraphs 中禁止再嵌套 section 或 paragraphs。中控负责标题、[R-*] 引用和排版。每段必须有来源，不能自行编造、缩写或扩展来源 ID。
只返回 JSON：{"paragraphs":[{"title":"小节标题，可为空；同一小节后续段落可留空","text":"一个自然语言段落","source_refs":["R-..."]}]}。""",
)


def _plain_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _validated_paragraphs(
    payload: dict, allowed_source_ids: set[str]
) -> tuple[list[dict], str]:
    raw_paragraphs = payload.get("paragraphs", [])
    if not isinstance(raw_paragraphs, list) or not 1 <= len(raw_paragraphs) <= 32:
        raise AgentPipelineError("Retrospective paragraphs 必须是一至三十二项数组")
    paragraphs = []
    total_length = 0
    for raw_paragraph in raw_paragraphs:
        if not isinstance(raw_paragraph, dict):
            raise AgentPipelineError("Retrospective paragraph 必须是对象")
        title = _plain_text(raw_paragraph.get("title"))
        if len(title) > 200:
            raise AgentPipelineError("Retrospective 小节标题过长")
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
        paragraphs.append(
            {"title": title, "text": text, "source_refs": normalized_refs}
        )
        total_length += len(text)
    if total_length > 24000:
        raise AgentPipelineError("Retrospective 正文超过 24000 字符")

    rendered = []
    last_title = ""
    for paragraph in paragraphs:
        title = paragraph["title"]
        if title and title != last_title:
            rendered.append(f"### {title}")
            last_title = title
        citations = ", ".join(paragraph["source_refs"])
        rendered.append(f"{paragraph['text']} [{citations}]")
    return paragraphs, "\n\n".join(rendered)


def validate(
    payload: dict,
    *,
    allowed_source_ids: set[str],
) -> str:
    _, markdown = _validated_paragraphs(payload, allowed_source_ids)
    return markdown
