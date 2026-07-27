"""Orchestrate daily profiles and weekly/monthly analysis reports."""

import datetime
import hashlib
import json
import logging
import re
from pathlib import Path

from .. import journal, settings
from ..agents import researcher, research_planner, retrospective, reviewer
from ..agents.base import (
    AgentOutputError,
    AgentPipelineError,
    cited_source_ids,
    invoke_agent,
)
from ..ai_client import (
    CONFIG_ERROR_MARKER,
    call_ai,
    search_web_once,
    third_party_search_available,
)
from ..file_lock import FileLock
from .context import (
    _analysis_report_path,
    _existing_logs,
    _information_briefings,
    _monthly_supporting_reports,
    _period_records,
    _record_chunks,
    _recent_summary_context,
    _referenced_records_context,
    _referenced_source_records,
    _log_without_summary,
)
from .profile_store import ProfileStore
from .store import AnalysisStore


logger = logging.getLogger(__name__)
_LONG_ID_PATTERN = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")
_MAX_AGENT_ATTEMPTS = 3
_MAX_STRUCTURE_REPAIRS = 2
_MAX_CONTENT_REVISIONS = 1
_MAX_GENERATOR_ATTEMPTS = 1 + _MAX_STRUCTURE_REPAIRS + _MAX_CONTENT_REVISIONS
_MAX_AGENT_INPUT_CHARACTERS = 120000
_MAX_RECORD_CHUNK_CHARACTERS = 30000
_MAX_PROFILE_CONTEXT_CHARACTERS = 24000
_PIPELINE_VERSION = 7


def _analysis_config_signature(model_config: settings.ModelDict) -> dict:
    """Return the non-secret effective configuration used by stage caches."""
    model = {
        key: model_config.get(key)
        for key in (
            "name",
            "model_id",
            "api_url",
            "search",
            "json_mode",
            "max_tokens",
            "temperature",
        )
    }
    third_search = settings.CONFIG.get("third_search", {})
    search = {
        key: third_search.get(key)
        for key in ("enabled", "api_url", "count", "timeout", "max_rounds")
    }
    return {"model": model, "third_search": search}


def _replace_id_substrings(value: object, replacements: dict[str, str]) -> object:
    if isinstance(value, str):
        return _LONG_ID_PATTERN.sub(
            lambda match: replacements.get(match.group(0), match.group(0)), value
        )
    if isinstance(value, list):
        return [_replace_id_substrings(item, replacements) for item in value]
    if isinstance(value, tuple):
        return tuple(_replace_id_substrings(item, replacements) for item in value)
    if isinstance(value, dict):
        return {
            key: _replace_id_substrings(item, replacements)
            for key, item in value.items()
        }
    return value


def _profile_input(profiles: list[dict]) -> tuple[list[dict], dict[str, str]]:
    selected = []
    size = 0
    for profile in sorted(
        profiles,
        key=lambda item: (
            str(item.get("last_observed", "")),
            str(item.get("updated_at", "")),
            str(item.get("id", "")),
        ),
        reverse=True,
    ):
        profile_size = len(json.dumps(profile, ensure_ascii=False))
        if profile_size > _MAX_PROFILE_CONTEXT_CHARACTERS:
            continue
        if size + profile_size > _MAX_PROFILE_CONTEXT_CHARACTERS:
            continue
        selected.append(profile)
        size += profile_size
    id_to_alias = {
        profile["id"]: f"P{index:03d}"
        for index, profile in enumerate(sorted(selected, key=lambda item: item["id"]), 1)
    }
    alias_to_id = {alias: entry_id for entry_id, alias in id_to_alias.items()}
    compact = [
        {
            "id": profile["id"],
            "category": profile["category"],
            "title": profile["title"],
            "statement": profile["statement"],
            "confidence": profile["confidence"],
            "source_refs": profile["source_refs"],
            "first_observed": profile["first_observed"],
            "last_observed": profile["last_observed"],
        }
        for profile in selected
    ]
    return _replace_id_substrings(compact, id_to_alias), alias_to_id


def _replace_profile_aliases(value: object, aliases: dict[str, str]) -> object:
    if isinstance(value, str):
        return aliases.get(value, value)
    if isinstance(value, list):
        return [_replace_profile_aliases(item, aliases) for item in value]
    if isinstance(value, dict):
        return {
            key: _replace_profile_aliases(item, aliases)
            for key, item in value.items()
        }
    return value


def summarize_diary(date: str, model_config: settings.ModelDict) -> tuple[str, bool]:
    """Generate the compact summary stored in a diary's summary region."""
    file_path = settings.DIARY_DIR / f"{date}.md"
    if not file_path.exists():
        return f"找不到 {date} 的记录。", False
    content = _log_without_summary(file_path.read_text(encoding="utf-8"))
    prompt = f"""[程序日记总结任务]
请总结 {date} 的日记。只输出要写入 <summary> 的 Markdown 正文，不要输出标题、标签、代码围栏或完成提示。

要求：
- 概括当天的重要事件、观点、决定、问题和进展，不逐条复述。
- 区分用户记录与引用的 AI 内容；AI 内容不能当作用户已经认可的观点。
- 保留重要具体信息，禁止编造、心理诊断和行为指导。

【{date} 原始日记】
{content}"""
    current_prompt = prompt
    summary = ""
    for attempt in range(1, _MAX_AGENT_ATTEMPTS + 1):
        summary, success, _, _, _ = call_ai(
            current_prompt, model_config, allowed_tools=()
        )
        if not success:
            return summary, False
        summary = summary.strip()
        fenced = re.fullmatch(
            r"```(?:markdown|md)?\s*\n?(.*?)\n?```",
            summary,
            re.DOTALL | re.IGNORECASE,
        )
        if fenced:
            summary = fenced.group(1).strip()
        summary = re.sub(r"</?summary>", "", summary, flags=re.IGNORECASE).strip()
        lines = summary.splitlines()
        if lines and re.fullmatch(r"#{1,6}\s+.*", lines[0].strip()):
            summary = "\n".join(lines[1:]).strip()
        errors = []
        if not summary.strip() or summary.strip() == "(AI 未给出最终回答)":
            errors.append("总结为空")
        if not errors:
            break
        if attempt == _MAX_AGENT_ATTEMPTS:
            return f"日记总结连续 {_MAX_AGENT_ATTEMPTS} 次未通过校验: {'；'.join(errors)}", False
        current_prompt = prompt + "\n\n【中控修订请求】\n" + json.dumps(
            _revision_context(attempt + 1, summary, errors, source="中控确定性校验"),
            ensure_ascii=False,
        )
    result = journal.update_summary_for_date(date, summary)
    if not result.endswith("总结已写入文档顶部。"):
        return result, False
    return summary, True


def _call_agent(
    spec,
    task: str,
    input_data: dict,
    model_config: settings.ModelDict,
    store: AnalysisStore,
    run_id: str,
    *,
    revision_context: dict | None = None,
    allowed_search_queries: list[str] | None = None,
) -> dict:
    logger.info("agent_start run=%s agent=%s", run_id, spec.name)
    try:
        input_size = len(json.dumps(input_data, ensure_ascii=False))
        revision_size = (
            len(json.dumps(revision_context, ensure_ascii=False))
            if revision_context
            else 0
        )
        if input_size + revision_size > _MAX_AGENT_INPUT_CHARACTERS:
            raise AgentPipelineError(
                f"{spec.name} 输入超过安全上限（{input_size + revision_size} > "
                f"{_MAX_AGENT_INPUT_CHARACTERS} 字符）"
            )
        payload = invoke_agent(
            spec,
            task,
            input_data,
            model_config,
            call_ai,
            revision_context=revision_context,
            allowed_search_queries=allowed_search_queries,
        )
    except AgentPipelineError as error:
        store.save_artifact(
            run_id,
            spec.name,
            {"response": error.response, "_telemetry": error.telemetry},
            status="failed",
            error=str(error),
        )
        logger.warning(
            "agent_failed run=%s agent=%s error_type=%s",
            run_id,
            spec.name,
            error.__class__.__name__,
        )
        raise
    telemetry = payload.get("_telemetry", {})
    logger.info(
        "agent_completed run=%s agent=%s duration_ms=%s total_tokens=%s cached_tokens=%s search_results=%s",
        run_id,
        spec.name,
        telemetry.get("duration_ms", 0),
        telemetry.get("usage", {}).get("total_tokens", 0),
        telemetry.get("usage", {}).get("cached_tokens", 0),
        telemetry.get("search_results", 0),
    )
    return payload


def _save_validation_failure(
    store: AnalysisStore, run_id: str, agent: str, payload: dict, error: Exception
) -> None:
    store.save_artifact(
        run_id, agent, payload, status="failed", error=str(error)
    )
    logger.warning(
        "agent_validation_failed run=%s agent=%s reason=%s",
        run_id,
        agent,
        str(error),
    )


def _revision_context(
    attempt: int,
    previous_output: object,
    feedback: object,
    *,
    source: str,
    maximum_attempts: int = _MAX_AGENT_ATTEMPTS,
) -> dict:
    """Build the common correction suffix while keeping the original prompt stable."""
    def model_visible(value: object) -> object:
        if isinstance(value, dict):
            return {
                key: model_visible(item)
                for key, item in value.items()
                if not str(key).startswith("_")
            }
        if isinstance(value, list):
            return [model_visible(item) for item in value]
        if isinstance(value, tuple):
            return tuple(model_visible(item) for item in value)
        return value

    return {
        "attempt": attempt,
        "maximum_attempts": maximum_attempts,
        "feedback_source": source,
        "problems_to_fix": model_visible(feedback),
        "rejected_previous_output": model_visible(previous_output),
    }


def _review_search_telemetry(telemetry: dict, sources: list[dict]) -> dict:
    """Keep only evidence the draft actually cites for the Reviewer."""
    source_keys = {researcher.canonical_url(source["url"]) for source in sources}
    evidence = []
    for item in telemetry.get("search_evidence", []):
        if not isinstance(item, dict) or not item.get("url"):
            continue
        if researcher.canonical_url(str(item["url"])) not in source_keys:
            continue
        evidence.append(
            {
                "query": str(item.get("query", "")),
                "title": str(item.get("title", "")),
                "url": str(item["url"]),
                "snippet": str(item.get("snippet", ""))[:800],
                "published": str(item.get("published", "")),
            }
        )
    return {
        "tool_calls": telemetry.get("tool_calls", {}),
        "search_results": telemetry.get("search_results", 0),
        "search_queries": telemetry.get("search_queries", []),
        "search_evidence": evidence,
    }


def _merge_search_evidence(
    accumulated: dict[tuple, dict], telemetry: dict
) -> None:
    """Retain auditable search results across bounded revisions in one run."""
    for item in telemetry.get("search_evidence", []):
        if not isinstance(item, dict) or not item.get("url"):
            continue
        url_key = researcher.canonical_url(str(item["url"]))
        if url_key[0] not in {"http", "https"} or not url_key[1]:
            continue
        accumulated.setdefault(url_key, item)


def _verified_source_options(
    topics: list[dict], evidence: list[dict], *, per_topic: int = 8
) -> list[dict]:
    """Return a compact exact-URL whitelist to guide a rejected revision."""
    results = []
    for topic in topics:
        query_key = re.sub(r"\s+", " ", topic["query"].strip()).casefold()
        options = []
        seen = set()
        for item in evidence:
            item_query = re.sub(
                r"\s+", " ", str(item.get("query", "")).strip()
            ).casefold()
            url = str(item.get("url", "")).strip()
            url_key = researcher.canonical_url(url)
            if not url or item_query != query_key or url_key in seen:
                continue
            # Only echo the exact URL.  Titles and snippets are untrusted web
            # text and must not be promoted into the controller's revision
            # instructions.
            options.append({"url": url})
            seen.add(url_key)
            if len(options) >= per_topic:
                break
        results.append({"topic_id": topic["topic_id"], "sources": options})
    return results


def _validated_agent_call(
    spec,
    task: str,
    input_data: dict,
    validator,
    model_config: settings.ModelDict,
    store: AnalysisStore,
    run_id: str,
    *,
    attempt_budget: list[int] | None = None,
):
    """Run one non-reviewed Agent stage with bounded output correction."""
    budget = attempt_budget if attempt_budget is not None else [_MAX_AGENT_ATTEMPTS]
    revision_context = None
    while budget[0] > 0:
        attempt = _MAX_AGENT_ATTEMPTS - budget[0] + 1
        budget[0] -= 1
        try:
            payload = _call_agent(
                spec,
                task,
                input_data,
                model_config,
                store,
                run_id,
                revision_context=revision_context,
            )
        except AgentOutputError as error:
            if budget[0] == 0:
                raise
            revision_context = _revision_context(
                attempt + 1,
                error.response,
                [str(error)],
                source="中控 JSON 解析",
            )
            continue
        try:
            result = validator(payload)
        except AgentPipelineError as error:
            _save_validation_failure(store, run_id, spec.name, payload, error)
            if budget[0] == 0:
                raise
            revision_context = _revision_context(
                attempt + 1,
                payload,
                [str(error)],
                source="中控确定性校验",
            )
            continue
        return payload, result
    raise RuntimeError("unreachable")


def _review_feedback(payload: dict) -> dict:
    return {
        "summary": payload.get("summary", ""),
        "required_changes": payload.get("required_changes", []),
        "unsupported_claims": payload.get("unsupported_claims", []),
        "entry_decisions": payload.get("entry_decisions", []),
        "topic_decisions": payload.get("topic_decisions", []),
    }


def _review(
    mode: str,
    section_payload: dict,
    entry_ids: set[str],
    review_context: dict,
    model_config: settings.ModelDict,
    store: AnalysisStore,
    run_id: str,
    *,
    topic_ids: set[str] | None = None,
    attempt_budget: list[int] | None = None,
) -> tuple[bool, dict[str, str], dict[str, str], list[str], dict]:
    expected_topic_ids = topic_ids or set()
    review_input = {
        "mode": mode,
        "section": section_payload,
        "valid_profile_temp_ids": sorted(entry_ids),
        "valid_research_topic_ids": sorted(expected_topic_ids),
        "review_context": review_context,
    }
    payload, result = _validated_agent_call(
        reviewer.SPEC,
        "审查该板块；逐项检查核心判断和来源，只报告会影响真实性、可追溯性或交付质量的实质问题。",
        review_input,
        lambda candidate: reviewer.validate(
            candidate,
            expected_entry_ids=entry_ids,
            expected_topic_ids=expected_topic_ids,
        ),
        model_config,
        store,
        run_id,
        attempt_budget=attempt_budget,
    )
    store.save_artifact(run_id, f"reviewer_{mode}", payload)
    return (*result, payload)


def _retrospective_section(
    base_input: dict,
    allowed_source_ids: set[str],
    current_source_ids: set[str],
    profile_aliases: dict[str, str],
    model_config: settings.ModelDict,
    store: AnalysisStore,
    run_id: str,
    *,
    visible_profiles_by_id: dict[str, dict] | None = None,
    task: str = (
        "生成整理与回顾板块和人物画像候选。"
        "没有值得长期保存的画像时返回空数组，不要凑数。"
    ),
) -> tuple[str, list[dict], dict[str, str]]:
    revision_context = None
    last_feedback: list[str] = []
    attempt = 0
    structure_repairs = 0
    content_revisions = 0
    while attempt < _MAX_GENERATOR_ATTEMPTS:
        attempt += 1
        try:
            payload = _call_agent(
                retrospective.SPEC,
                task,
                base_input,
                model_config,
                store,
                run_id,
                revision_context=revision_context,
            )
        except AgentOutputError as error:
            if (
                structure_repairs >= _MAX_STRUCTURE_REPAIRS
                or attempt == _MAX_GENERATOR_ATTEMPTS
            ):
                raise
            structure_repairs += 1
            revision_context = _revision_context(
                attempt + 1,
                error.response,
                [str(error)],
                source="中控 JSON 解析",
                maximum_attempts=_MAX_GENERATOR_ATTEMPTS,
            )
            continue
        payload = _replace_profile_aliases(payload, profile_aliases)
        try:
            markdown, entries = retrospective.validate(
                payload,
                allowed_source_ids=allowed_source_ids,
                current_source_ids=current_source_ids,
                visible_profile_ids=set(profile_aliases.values()),
                visible_profiles=visible_profiles_by_id,
            )
        except AgentPipelineError as error:
            _save_validation_failure(
                store, run_id, retrospective.SPEC.name, payload, error
            )
            if (
                structure_repairs >= _MAX_STRUCTURE_REPAIRS
                or attempt == _MAX_GENERATOR_ATTEMPTS
            ):
                raise
            structure_repairs += 1
            revision_context = _revision_context(
                attempt + 1,
                payload,
                [str(error)],
                source="中控确定性校验",
                maximum_attempts=_MAX_GENERATOR_ATTEMPTS,
            )
            continue

        normalized_payload = {
            "markdown": markdown,
            "structured_sections": payload.get("sections", []),
            "profile_entries": entries,
        }
        cited_ids = cited_source_ids(markdown)
        cited_ids.update(ref for entry in entries for ref in entry["source_refs"])
        reviewable_records = [
            *base_input["records"],
            *base_input.get("referenced_records", []),
        ]
        review_context = {
            "period": base_input["period"],
            "records": [
                record
                for record in reviewable_records
                if record["source_id"] in cited_ids
            ],
            "historical_profiles": base_input["historical_profiles"],
        }
        passed, decisions, _, last_feedback, review_payload = _review(
            "retrospective_review",
            normalized_payload,
            {entry["temp_id"] for entry in entries},
            review_context,
            model_config,
            store,
            run_id,
        )
        if passed:
            store.save_artifact(
                run_id,
                retrospective.SPEC.name,
                {
                    **normalized_payload,
                    "entry_decisions": decisions,
                    "_telemetry": payload.get("_telemetry", {}),
                },
            )
            return markdown, entries, decisions

        error = AgentPipelineError(
            "整理与回顾未通过审查: " + "; ".join(last_feedback)
        )
        _save_validation_failure(
            store, run_id, retrospective.SPEC.name, normalized_payload, error
        )
        if (
            content_revisions >= _MAX_CONTENT_REVISIONS
            or attempt == _MAX_GENERATOR_ATTEMPTS
        ):
            raise error
        content_revisions += 1
        revision_context = _revision_context(
            attempt + 1,
            {
                "sections": payload.get("sections", []),
                "profile_entries": entries,
            },
            _review_feedback(review_payload),
            source="Reviewer 实质审查",
            maximum_attempts=_MAX_GENERATOR_ATTEMPTS,
        )
    raise AgentPipelineError("整理与回顾修订次数耗尽: " + "; ".join(last_feedback))


def _merge_chunk_profile_entries(
    chunk_results: list[tuple[str, list[dict], dict[str, str]]],
) -> tuple[str, list[dict], dict[str, str]]:
    markdown_sections = []
    entries = []
    decisions = {}
    signatures = set()
    superseded_ids = set()
    candidates_with_decisions = []
    for markdown, candidates, chunk_decisions in chunk_results:
        markdown_sections.append(markdown)
        candidates_with_decisions.extend(
            (
                candidate,
                chunk_decisions.get(candidate["temp_id"], "rejected"),
            )
            for candidate in candidates
        )
    candidates_with_decisions.sort(
        key=lambda item: item[1] != "accepted"
    )
    for candidate, decision in candidates_with_decisions:
        signature = (
            candidate["category"],
            re.sub(r"\s+", "", candidate["title"]).casefold(),
            re.sub(r"\s+", "", candidate["statement"]).casefold(),
        )
        supersedes_id = candidate.get("supersedes_id")
        if (
            signature in signatures
            or (supersedes_id and supersedes_id in superseded_ids)
            or len(entries) == 12
        ):
            continue
        new_temp_id = f"p{len(entries) + 1}"
        entries.append({**candidate, "temp_id": new_temp_id})
        decisions[new_temp_id] = decision
        signatures.add(signature)
        if supersedes_id:
            superseded_ids.add(supersedes_id)
    return "\n\n".join(markdown_sections), entries, decisions


def _retrospective_with_input_budget(
    base_input: dict,
    allowed_source_ids: set[str],
    current_source_ids: set[str],
    profile_aliases: dict[str, str],
    model_config: settings.ModelDict,
    store: AnalysisStore,
    run_id: str,
    *,
    visible_profiles_by_id: dict[str, dict] | None = None,
    task: str = (
        "生成整理与回顾板块和人物画像候选。"
        "没有值得长期保存的画像时返回空数组，不要凑数。"
    ),
) -> tuple[str, list[dict], dict[str, str]]:
    if len(json.dumps(base_input, ensure_ascii=False)) <= _MAX_AGENT_INPUT_CHARACTERS:
        return _retrospective_section(
            base_input,
            allowed_source_ids,
            current_source_ids,
            profile_aliases,
            model_config,
            store,
            run_id,
            visible_profiles_by_id=visible_profiles_by_id,
            task=task,
        )

    records = [*base_input["records"], *base_input.get("referenced_records", [])]
    if not records:
        raise AgentPipelineError("Retrospective 固定上下文超过安全上限")
    chunks = _record_chunks(records, _MAX_RECORD_CHUNK_CHARACTERS)
    chunk_results = []
    for index, chunk in enumerate(chunks, 1):
        chunk_ids = {record["source_id"] for record in chunk}
        chunk_current_ids = chunk_ids & current_source_ids
        chunk_input = {
            **base_input,
            "records": [
                record for record in chunk if record["source_id"] in current_source_ids
            ],
            "referenced_records": [
                record for record in chunk if record["source_id"] not in current_source_ids
            ],
            "chunk": {"index": index, "total": len(chunks)},
        }
        chunk_results.append(
            _retrospective_section(
                chunk_input,
                allowed_source_ids
                & (chunk_ids | (allowed_source_ids - current_source_ids)),
                chunk_current_ids,
                profile_aliases,
                model_config,
                store,
                run_id,
                visible_profiles_by_id=visible_profiles_by_id,
                task=(
                    task
                    + f" 当前只处理第 {index}/{len(chunks)} 个原文分块；"
                    "不得声称覆盖未提供的分块。"
                ),
            )
        )
    markdown, entries, decisions = _merge_chunk_profile_entries(chunk_results)
    store.save_artifact(
        run_id,
        retrospective.SPEC.name,
        {
            "markdown": markdown,
            "profile_entries": entries,
            "entry_decisions": decisions,
            "input_chunks": len(chunks),
        },
    )
    return markdown, entries, decisions


def _research_topics(
    planner_input: dict,
    current_source_ids: set[str],
    model_config: settings.ModelDict,
    store: AnalysisStore,
    run_id: str,
) -> list[dict]:
    payload, topics = _validated_agent_call(
        research_planner.SPEC,
        "选择少量记录驱动或信息雷达驱动的公开研究主题。",
        planner_input,
        lambda candidate: research_planner.validate(
            candidate, current_source_ids
        ),
        model_config,
        store,
        run_id,
    )
    store.save_artifact(
        run_id,
        research_planner.SPEC.name,
        {"topics": topics, "_telemetry": payload.get("_telemetry", {})},
    )
    return topics


def _native_research_section(
    topics: list[dict],
    information_leads: str,
    current_source_ids: set[str],
    model_config: settings.ModelDict,
    store: AnalysisStore,
    run_id: str,
) -> str:
    research_input = {
        "research_topics": topics,
        "information_leads": information_leads,
    }
    revision_context = None
    last_feedback: list[str] = []
    accumulated_evidence: dict[tuple, dict] = {}
    attempt = 0
    structure_repairs = 0
    content_revisions = 0
    while attempt < _MAX_GENERATOR_ATTEMPTS:
        attempt += 1
        try:
            payload = _call_agent(
                researcher.NATIVE_SEARCH_SPEC,
                "逐项联网查证并返回语义段落；本次调用即使是修订稿也必须重新执行 web_search。",
                research_input,
                model_config,
                store,
                run_id,
                revision_context=revision_context,
                allowed_search_queries=[topic["query"] for topic in topics],
            )
        except AgentOutputError as error:
            _merge_search_evidence(accumulated_evidence, error.telemetry)
            if (
                structure_repairs >= _MAX_STRUCTURE_REPAIRS
                or attempt == _MAX_GENERATOR_ATTEMPTS
            ):
                raise
            structure_repairs += 1
            revision_context = _revision_context(
                attempt + 1,
                error.response,
                {
                    "validation_error": str(error),
                    "verified_source_options": _verified_source_options(
                        topics, list(accumulated_evidence.values())
                    ),
                },
                source="中控 JSON 解析",
                maximum_attempts=_MAX_GENERATOR_ATTEMPTS,
            )
            continue
        telemetry = payload.get("_telemetry", {})
        _merge_search_evidence(accumulated_evidence, telemetry)
        try:
            used_search = bool(
                telemetry.get("web_citations", 0)
                or telemetry.get("search_results", 0)
                or telemetry.get("search_evidence", [])
            )
            if not used_search:
                raise AgentPipelineError("领域研究没有实际执行联网搜索")
            drafts, evidence = researcher.validate_native(
                payload,
                topics,
                list(accumulated_evidence.values()),
                current_source_ids,
            )
            supported_topic_ids = {
                draft["topic_id"]
                for draft in drafts
                if draft["status"] == "supported"
            }
            supported_topics = [
                topic
                for topic in topics
                if topic["topic_id"] in supported_topic_ids
            ]
            rendered_markdown, sources = researcher.render_grounded(
                drafts, topics, evidence
            )
        except AgentPipelineError as error:
            _save_validation_failure(
                store, run_id, researcher.SPEC.name, payload, error
            )
            if (
                structure_repairs >= _MAX_STRUCTURE_REPAIRS
                or attempt == _MAX_GENERATOR_ATTEMPTS
            ):
                raise
            structure_repairs += 1
            revision_context = _revision_context(
                attempt + 1,
                payload,
                {
                    "validation_error": str(error),
                    "verified_source_options": _verified_source_options(
                        topics, list(accumulated_evidence.values())
                    ),
                },
                source="中控确定性校验",
                maximum_attempts=_MAX_GENERATOR_ATTEMPTS,
            )
            continue

        telemetry = {
            **telemetry,
            "search_evidence": list(accumulated_evidence.values()),
        }
        normalized_payload = {
            "markdown": rendered_markdown,
            "sources": sources,
            "structured_topics": drafts,
        }
        passed, _, topic_decisions, last_feedback, review_payload = _review(
            "research_review",
            normalized_payload,
            set(),
            {
                "research_topics": supported_topics,
                "information_leads": information_leads,
                "search_telemetry": _review_search_telemetry(telemetry, sources),
            },
            model_config,
            store,
            run_id,
            topic_ids=supported_topic_ids,
        )
        accepted_topic_ids = {
            topic_id
            for topic_id, status in topic_decisions.items()
            if status == "accepted"
        }
        if accepted_topic_ids:
            final_markdown, final_sources = researcher.render_grounded(
                drafts,
                topics,
                evidence,
                accepted_topic_ids=accepted_topic_ids,
            )
            store.save_artifact(
                run_id,
                researcher.SPEC.name,
                {
                    "markdown": final_markdown,
                    "sources": final_sources,
                    "structured_topics": drafts,
                    "topic_decisions": topic_decisions,
                    "dropped_topic_ids": [
                        topic["topic_id"]
                        for topic in topics
                        if topic["topic_id"] not in accepted_topic_ids
                    ],
                    "_telemetry": telemetry,
                },
            )
            if not passed:
                logger.info(
                    "research_topics_dropped run=%s accepted=%s dropped=%s",
                    run_id,
                    len(accepted_topic_ids),
                    len(topics) - len(accepted_topic_ids),
                )
            return final_markdown

        error = AgentPipelineError(
            "领域研究没有主题通过审查: " + "; ".join(last_feedback)
        )
        _save_validation_failure(
            store, run_id, researcher.SPEC.name, normalized_payload, error
        )
        if (
            content_revisions >= _MAX_CONTENT_REVISIONS
            or attempt == _MAX_GENERATOR_ATTEMPTS
        ):
            raise error
        content_revisions += 1
        revision_context = _revision_context(
            attempt + 1,
            {"topics": drafts},
            _review_feedback(review_payload),
            source="Reviewer 实质审查",
            maximum_attempts=_MAX_GENERATOR_ATTEMPTS,
        )
    raise AgentPipelineError("领域研究修订次数耗尽: " + "; ".join(last_feedback))


def _valid_cached_research_evidence(
    cached_payload: dict | None, topics: list[dict]
) -> tuple[list[dict], list[dict], dict] | None:
    if not isinstance(cached_payload, dict) or cached_payload.get("topics") != topics:
        return None
    usable_topics = cached_payload.get("usable_topics")
    evidence = cached_payload.get("evidence")
    telemetry = cached_payload.get("_telemetry")
    if not isinstance(usable_topics, list) or not isinstance(evidence, list):
        return None
    if not usable_topics or not evidence or not isinstance(telemetry, dict):
        return None
    original_topics = {topic.get("topic_id"): topic for topic in topics}
    topic_ids = {topic.get("topic_id") for topic in usable_topics}
    if (
        len(topic_ids) != len(usable_topics)
        or any(
            original_topics.get(topic.get("topic_id")) != topic
            for topic in usable_topics
        )
    ):
        return None
    seen = set()
    evidence_topic_ids = set()
    for item in evidence:
        if not isinstance(item, dict):
            return None
        source_id = str(item.get("source_id", ""))
        topic_id = item.get("topic_id")
        url = str(item.get("url", ""))
        url_key = researcher.canonical_url(url)
        if (
            not re.fullmatch(r"W-Q\d{3}-\d{3}", source_id)
            or source_id in seen
            or topic_id not in topic_ids
            or not source_id.startswith(f"W-{topic_id}-")
            or re.search(r"[\x00-\x20\x7f]", url)
            or url_key[0] not in {"http", "https"}
            or not url_key[1]
            or any(key not in item for key in ("title", "snippet", "published"))
        ):
            return None
        seen.add(source_id)
        evidence_topic_ids.add(topic_id)
    if evidence_topic_ids != topic_ids:
        return None
    return usable_topics, evidence, telemetry


def _collect_research_evidence(
    topics: list[dict],
    store: AnalysisStore,
    run_id: str,
    cached: tuple[str, dict] | None = None,
) -> tuple[list[dict], list[dict], dict]:
    """Search each fixed query once and assign controller-owned evidence IDs."""
    if cached:
        validated = _valid_cached_research_evidence(cached[1], topics)
        if validated:
            usable_topics, evidence, telemetry = validated
            store.save_artifact(
                run_id,
                "research_search",
                {
                    "topics": topics,
                    "usable_topics": usable_topics,
                    "evidence": evidence,
                    "_telemetry": telemetry,
                    "_cache": {"hit": True, "source_run_id": cached[0]},
                },
            )
            logger.info(
                "agent_cache_hit run=%s agent=research_search source_run=%s",
                run_id,
                cached[0],
            )
            return usable_topics, evidence, telemetry

    evidence = []
    usable_topics = []
    search_queries = []
    search_results = 0
    for topic in topics:
        query = topic["query"]
        search_queries.append(query)
        result, error = search_web_once(query)
        if error:
            payload = {
                "topics": topics,
                "usable_topics": usable_topics,
                "evidence": evidence,
                "_telemetry": {
                    "tool_calls": {"web_search": len(search_queries)},
                    "search_queries": search_queries,
                    "search_results": search_results,
                    "search_evidence": evidence,
                },
            }
            store.save_artifact(
                run_id, "research_search", payload, status="failed", error=error
            )
            raise AgentPipelineError(error)
        search_results += result.result_count
        topic_evidence = []
        seen_urls = set()
        for item in result.evidence:
            url = str(item.get("url", "")).strip()
            url_key = researcher.canonical_url(url)
            if (
                re.search(r"[\x00-\x20\x7f]", url)
                or url_key[0] not in {"http", "https"}
                or not url_key[1]
                or url_key in seen_urls
            ):
                continue
            seen_urls.add(url_key)
            source_id = f"W-{topic['topic_id']}-{len(topic_evidence) + 1:03d}"
            topic_evidence.append(
                {
                    "source_id": source_id,
                    "topic_id": topic["topic_id"],
                    "query": query,
                    "title": str(item.get("title", ""))[:300],
                    "url": url,
                    "snippet": str(item.get("snippet", ""))[:800],
                    "published": str(item.get("published", ""))[:80],
                }
            )
        if topic_evidence:
            usable_topics.append(topic)
            evidence.extend(topic_evidence)

    telemetry = {
        "tool_calls": {"web_search": len(search_queries)},
        "search_queries": search_queries,
        "search_results": search_results,
        "search_evidence": evidence,
    }
    payload = {
        "topics": topics,
        "usable_topics": usable_topics,
        "dropped_topic_ids": [
            topic["topic_id"] for topic in topics if topic not in usable_topics
        ],
        "evidence": evidence,
        "_telemetry": telemetry,
    }
    if not usable_topics:
        error = "所有研究主题的固定查询都没有返回可验证结果"
        store.save_artifact(
            run_id, "research_search", payload, status="failed", error=error
        )
        raise AgentPipelineError(error)
    store.save_artifact(run_id, "research_search", payload)
    logger.info(
        "research_search_completed run=%s queries=%s results=%s usable_topics=%s",
        run_id,
        len(search_queries),
        search_results,
        len(usable_topics),
    )
    return usable_topics, evidence, telemetry


def _grounded_research_section(
    topics: list[dict],
    current_source_ids: set[str],
    model_config: settings.ModelDict,
    store: AnalysisStore,
    run_id: str,
    cached_search: tuple[str, dict] | None = None,
) -> str:
    usable_topics, evidence, search_telemetry = _collect_research_evidence(
        topics, store, run_id, cached_search
    )
    research_input = {
        "research_topics": usable_topics,
        "evidence_sources": [
            {
                "source_id": item["source_id"],
                "topic_id": item["topic_id"],
                "title": item["title"],
                "snippet": item["snippet"],
                "published": item["published"],
            }
            for item in evidence
        ],
    }
    revision_context = None
    last_feedback: list[str] = []
    attempt = 0
    structure_repairs = 0
    content_revisions = 0
    while attempt < _MAX_GENERATOR_ATTEMPTS:
        attempt += 1
        try:
            payload = _call_agent(
                researcher.SPEC,
                "基于中控已经检索的证据返回逐主题语义段落；只选择 W-* 证据 ID，不要自行输出 URL。",
                research_input,
                model_config,
                store,
                run_id,
                revision_context=revision_context,
            )
        except AgentOutputError as error:
            if (
                structure_repairs >= _MAX_STRUCTURE_REPAIRS
                or attempt == _MAX_GENERATOR_ATTEMPTS
            ):
                raise
            structure_repairs += 1
            revision_context = _revision_context(
                attempt + 1,
                error.response,
                [str(error)],
                source="中控 JSON 解析",
                maximum_attempts=_MAX_GENERATOR_ATTEMPTS,
            )
            continue
        try:
            drafts = researcher.validate_grounded(
                payload, usable_topics, evidence, current_source_ids
            )
            supported_topic_ids = {
                draft["topic_id"]
                for draft in drafts
                if draft["status"] == "supported"
            }
            supported_topics = [
                topic
                for topic in usable_topics
                if topic["topic_id"] in supported_topic_ids
            ]
            rendered_markdown, sources = researcher.render_grounded(
                drafts, usable_topics, evidence
            )
        except AgentPipelineError as error:
            _save_validation_failure(
                store, run_id, researcher.SPEC.name, payload, error
            )
            if (
                structure_repairs >= _MAX_STRUCTURE_REPAIRS
                or attempt == _MAX_GENERATOR_ATTEMPTS
            ):
                raise
            structure_repairs += 1
            revision_context = _revision_context(
                attempt + 1,
                payload,
                [str(error)],
                source="中控确定性校验",
                maximum_attempts=_MAX_GENERATOR_ATTEMPTS,
            )
            continue

        normalized_payload = {
            "markdown": rendered_markdown,
            "sources": sources,
            "structured_topics": drafts,
        }
        passed, _, topic_decisions, last_feedback, review_payload = _review(
            "research_review",
            normalized_payload,
            set(),
            {
                "research_topics": supported_topics,
                "search_telemetry": _review_search_telemetry(
                    search_telemetry, sources
                ),
            },
            model_config,
            store,
            run_id,
            topic_ids=supported_topic_ids,
        )
        accepted_topic_ids = {
            topic_id
            for topic_id, status in topic_decisions.items()
            if status == "accepted"
        }
        if accepted_topic_ids:
            final_markdown, final_sources = researcher.render_grounded(
                drafts,
                usable_topics,
                evidence,
                accepted_topic_ids=accepted_topic_ids,
            )
            model_telemetry = payload.get("_telemetry", {})
            store.save_artifact(
                run_id,
                researcher.SPEC.name,
                {
                    "markdown": final_markdown,
                    "sources": final_sources,
                    "structured_topics": drafts,
                    "topic_decisions": topic_decisions,
                    "dropped_topic_ids": [
                        topic["topic_id"]
                        for topic in usable_topics
                        if topic["topic_id"] not in accepted_topic_ids
                    ],
                    "_telemetry": {
                        **model_telemetry,
                        **search_telemetry,
                    },
                },
            )
            if not passed:
                logger.info(
                    "research_topics_dropped run=%s accepted=%s dropped=%s",
                    run_id,
                    len(accepted_topic_ids),
                    len(usable_topics) - len(accepted_topic_ids),
                )
            return final_markdown

        error = AgentPipelineError(
            "领域研究没有主题通过审查: " + "; ".join(last_feedback)
        )
        _save_validation_failure(
            store,
            run_id,
            researcher.SPEC.name,
            {
                "structured_topics": drafts,
                "rendered_markdown": rendered_markdown,
                "sources": sources,
            },
            error,
        )
        if (
            content_revisions >= _MAX_CONTENT_REVISIONS
            or attempt == _MAX_GENERATOR_ATTEMPTS
        ):
            raise error
        content_revisions += 1
        revision_context = _revision_context(
            attempt + 1,
            {"topics": drafts},
            _review_feedback(review_payload),
            source="Reviewer 实质审查",
            maximum_attempts=_MAX_GENERATOR_ATTEMPTS,
        )
    raise AgentPipelineError("领域研究修订次数耗尽: " + "; ".join(last_feedback))


def _research_section(
    topics: list[dict],
    information_leads: str,
    current_source_ids: set[str],
    model_config: settings.ModelDict,
    store: AnalysisStore,
    run_id: str,
    cached_search: tuple[str, dict] | None = None,
) -> str:
    if third_party_search_available():
        return _grounded_research_section(
            topics,
            current_source_ids,
            model_config,
            store,
            run_id,
            cached_search,
        )
    return _native_research_section(
        topics,
        information_leads,
        current_source_ids,
        model_config,
        store,
        run_id,
    )


def _observed_dates(
    entries: list[dict], profiles_by_id: dict[str, dict], store: AnalysisStore
) -> None:
    refs = list(
        dict.fromkeys(ref for entry in entries for ref in entry["source_refs"])
    )
    source_dates = {
        source["source_id"]: source["source_date"]
        for source in store.source_records(refs)
    }
    for entry in entries:
        dates = [source_dates[ref] for ref in entry["source_refs"] if ref in source_dates]
        if not dates:
            raise AgentPipelineError("人物画像来源无法映射到记录日期")
        first_observed = min(dates)
        last_observed = max(dates)
        previous = profiles_by_id.get(entry.get("supersedes_id"))
        if previous:
            first_observed = min(first_observed, previous["first_observed"])
        entry["first_observed"] = first_observed
        entry["last_observed"] = last_observed


def _source_appendix(markdown: str, store: AnalysisStore) -> str:
    cited = sorted(cited_source_ids(markdown))
    records = {record["source_id"]: record for record in store.source_records(cited)}
    lines = ["## 来源索引"]
    for source_id in cited:
        record = records.get(source_id)
        if not record:
            continue
        lines.append(
            f"- [{source_id}] {record['source_date']} {record['source_time']} "
            f"— `{record['relative_path']}` 第 {record['record_index']} 条记录"
        )
    return "\n".join(lines)


def generate_daily_profile(
    date: datetime.date,
    model_config: settings.ModelDict,
    *,
    trigger: str = "scheduled",
) -> tuple[str, bool]:
    """Review one closed diary day and activate only validated profile changes."""
    if trigger not in {"scheduled", "retry"}:
        return f"未知每日人物画像触发方式: {trigger}", False
    logs = _existing_logs(date, date)
    if not logs:
        return f"{date:%Y-%m-%d} 没有日记记录。", False
    records = _period_records(logs)
    if not records:
        return "日记中没有可识别的标准记录。", False

    report_lock = FileLock.acquire(settings.ANALYSIS_DIR / ".report.lock")
    if report_lock is None:
        return "另一个分析任务正在生成，请稍后重试。", False
    store: AnalysisStore | None = None
    run_id: str | None = None
    try:
        store = AnalysisStore()
        profile_store = ProfileStore()
        current_source_ids = {record["source_id"] for record in records}
        profiles = profile_store.active_profiles(date.isoformat())
        historical_source_ids = {
            ref for profile in profiles for ref in profile["source_refs"]
        }
        compact_profiles, profile_aliases = _profile_input(profiles)
        profiles_by_id = {
            profile["id"]: profile
            for profile in profiles
            if profile["id"] in profile_aliases.values()
        }
        referenced_records = _referenced_source_records(logs)
        referenced_sources = _referenced_records_context(referenced_records)
        referenced_source_ids = {
            record["source_id"] for record in referenced_records
        }
        recent_summaries = _recent_summary_context(date)
        snapshot = {
            "pipeline_version": _PIPELINE_VERSION,
            "analysis_config": _analysis_config_signature(model_config),
            "kind": "daily_profile",
            "records": records,
            "referenced_records": referenced_records,
            "profiles": profiles,
            "referenced_sources": referenced_sources,
            "recent_summaries": recent_summaries,
        }
        input_hash = hashlib.sha256(
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        run_id = store.start_run(
            "daily_profile",
            date.isoformat(),
            date.isoformat(),
            "auto",
            model_config.get("name", ""),
            input_hash,
            trigger=trigger,
        )
        logger.info("daily_profile_started run=%s date=%s", run_id, date)
        store.save_sources(run_id, [*records, *referenced_records])
        retrospective_input = {
            "period": {
                "kind": "daily_profile",
                "start": date.isoformat(),
                "end": date.isoformat(),
            },
            "records": records,
            "referenced_records": referenced_records,
            "historical_profiles": compact_profiles,
            "historical_profile_omissions": len(profiles) - len(compact_profiles),
            "recent_summaries": recent_summaries,
            "supporting_reports": "（每日人物画像不读取周期报告）",
        }
        _, entries, decisions = _retrospective_with_input_budget(
            retrospective_input,
            current_source_ids | referenced_source_ids | historical_source_ids,
            current_source_ids,
            profile_aliases,
            model_config,
            store,
            run_id,
            visible_profiles_by_id=profiles_by_id,
            task=(
                "只为昨日记录生成内部审查摘要和人物画像候选；摘要不会作为日报交付。"
                "画像仍只保存相对稳定、值得跨日比较的内容，没有合适更新时返回空数组。"
            ),
        )
        _observed_dates(entries, profiles_by_id, store)
        _, profile_snapshot = profile_store.commit_entries(
            run_id=run_id,
            period_start=date.isoformat(),
            period_end=date.isoformat(),
            entries=entries,
            decisions=decisions,
        )
        try:
            store.complete_run(run_id)
        except Exception:
            profile_store.restore(profile_snapshot)
            raise
        accepted = sum(
            decisions.get(entry["temp_id"]) == "accepted" for entry in entries
        )
        logger.info(
            "daily_profile_completed run=%s date=%s accepted=%s",
            run_id,
            date,
            accepted,
        )
        return f"{date:%Y-%m-%d} 人物画像已更新（接受 {accepted} 项）。", True
    except Exception as error:
        message = str(error) or error.__class__.__name__
        if store is not None and run_id is not None:
            try:
                store.fail_run(run_id, message)
            except Exception as state_error:
                message += f"；保存失败状态时又发生异常: {state_error}"
        logger.error(
            "daily_profile_failed run=%s error_type=%s",
            run_id or "not-started",
            error.__class__.__name__,
        )
        return f"人物画像更新失败: {message}", False
    finally:
        report_lock.release()


def generate_analysis_report(
    kind: str,
    anchor: datetime.date,
    model_config: settings.ModelDict,
    *,
    origin: str = "manual",
    trigger: str | None = None,
) -> tuple[str, bool, Path | None]:
    """Generate one weekly/monthly two-section report and atomically save it."""
    if kind == "weekly":
        start = anchor - datetime.timedelta(days=anchor.weekday())
        end = start + datetime.timedelta(days=6)
        report_name = f"{start:%Y-%m-%d} 至 {end:%Y-%m-%d} 分析周报"
    elif kind == "monthly":
        start = anchor.replace(day=1)
        next_month = (start.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        end = next_month - datetime.timedelta(days=1)
        report_name = f"{start:%Y年%m月} 分析月报"
    else:
        return "分析报告只支持 weekly 或 monthly。", False, None
    if origin not in {"manual", "auto"}:
        return f"未知报告来源: {origin}", False, None
    trigger = trigger or ("manual" if origin == "manual" else "scheduled")

    logs = _existing_logs(start, end)
    if not logs:
        return f"{start:%Y-%m-%d} 至 {end:%Y-%m-%d} 没有日记记录。", False, None
    records = _period_records(logs)
    if not records:
        return "日记中没有可识别的标准记录。", False, None
    if not third_party_search_available():
        return (
            f"{CONFIG_ERROR_MARKER} 周报和月报需要启用第三方搜索，"
            "以便中控逐条执行查询并审计来源。",
            False,
            None,
        )

    report_path = _analysis_report_path(kind, start, end, origin)
    report_lock = FileLock.acquire(settings.ANALYSIS_DIR / ".report.lock")
    if report_lock is None:
        return "另一个分析报告正在生成，请稍后重试。", False, None
    store: AnalysisStore | None = None
    run_id: str | None = None
    try:
        store = AnalysisStore()
        profile_store = ProfileStore()
        current_source_ids = {record["source_id"] for record in records}
        profiles = profile_store.active_profiles(end.isoformat())
        historical_source_ids = {
            ref for profile in profiles for ref in profile["source_refs"]
        }
        referenced_records = _referenced_source_records(logs)
        referenced_sources = _referenced_records_context(referenced_records)
        referenced_source_ids = {
            record["source_id"] for record in referenced_records
        }
        allowed_source_ids = (
            current_source_ids | referenced_source_ids | historical_source_ids
        )
        recent_summaries = _recent_summary_context(start)
        supporting_reports = (
            _monthly_supporting_reports(start, end)
            if kind == "monthly"
            else "（周报不读取下级周期报告）"
        )
        information_leads = _information_briefings(start, end)
        snapshot = {
            "pipeline_version": _PIPELINE_VERSION,
            "analysis_config": _analysis_config_signature(model_config),
            "records": records,
            "referenced_records": referenced_records,
            "profiles": profiles,
            "referenced_sources": referenced_sources,
            "recent_summaries": recent_summaries,
            "supporting_reports": supporting_reports,
            "information_leads": information_leads,
        }
        input_hash = hashlib.sha256(
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        run_id = store.start_run(
            kind,
            start.isoformat(),
            end.isoformat(),
            origin,
            model_config.get("name", ""),
            input_hash,
            trigger=trigger,
        )
        logger.info(
            "analysis_started run=%s kind=%s origin=%s trigger=%s period=%s..%s",
            run_id,
            kind,
            origin,
            trigger,
            start,
            end,
        )
        store.save_sources(run_id, [*records, *referenced_records])

        cache_arguments = (
            input_hash,
            kind,
            start.isoformat(),
            end.isoformat(),
            origin,
            model_config.get("name", ""),
        )

        compact_profiles, profile_aliases = _profile_input(profiles)
        profiles_by_id = {
            profile["id"]: profile
            for profile in profiles
            if profile["id"] in profile_aliases.values()
        }
        retrospective_input = {
            "period": {"kind": kind, "start": start.isoformat(), "end": end.isoformat()},
            "records": records,
            "referenced_records": referenced_records,
            "historical_profiles": compact_profiles,
            "historical_profile_omissions": len(profiles) - len(compact_profiles),
            "recent_summaries": recent_summaries,
            "supporting_reports": supporting_reports,
        }
        cached_retrospective = store.reusable_artifact(
            *cache_arguments, retrospective.SPEC.name
        )
        cache_run_id = None
        if cached_retrospective and isinstance(
            cached_retrospective[1].get("entry_decisions"), dict
        ):
            cache_run_id, cached_payload = cached_retrospective
            retrospective_markdown = str(cached_payload.get("markdown", "")).strip()
            entries = cached_payload.get("profile_entries", [])
            decisions = cached_payload["entry_decisions"]
            if not retrospective_markdown or not isinstance(entries, list):
                cache_run_id = None
        if cache_run_id is None:
            retrospective_markdown, entries, decisions = (
                _retrospective_with_input_budget(
                    retrospective_input,
                    allowed_source_ids,
                    current_source_ids,
                    profile_aliases,
                    model_config,
                    store,
                    run_id,
                    visible_profiles_by_id=profiles_by_id,
                )
            )
        else:
            store.save_artifact(
                run_id,
                retrospective.SPEC.name,
                {
                    "markdown": retrospective_markdown,
                    "profile_entries": entries,
                    "entry_decisions": decisions,
                    "_cache": {"hit": True, "source_run_id": cache_run_id},
                },
            )
            logger.info(
                "agent_cache_hit run=%s agent=%s source_run=%s",
                run_id,
                retrospective.SPEC.name,
                cache_run_id,
            )
        _observed_dates(entries, profiles_by_id, store)

        planner_input = {
            "period": {"kind": kind, "start": start.isoformat(), "end": end.isoformat()},
            "records": records,
            "retrospective": retrospective_markdown,
            "daily_information_briefings": information_leads,
        }
        if len(json.dumps(planner_input, ensure_ascii=False)) > _MAX_AGENT_INPUT_CHARACTERS:
            planner_input["records"] = []
            planner_input["record_input_note"] = (
                "原始记录已由 Retrospective 分块审查；请从带 R-* 引用的 retrospective "
                "中选择记录驱动主题，source_refs 只能复制其中出现的来源 ID。"
            )
        cached_planner = store.reusable_artifact(
            *cache_arguments, research_planner.SPEC.name
        )
        if cached_planner and cached_planner[0] == cache_run_id and isinstance(
            cached_planner[1].get("topics"), list
        ):
            topics = cached_planner[1]["topics"]
            store.save_artifact(
                run_id,
                research_planner.SPEC.name,
                {
                    "topics": topics,
                    "_cache": {"hit": True, "source_run_id": cache_run_id},
                },
            )
            logger.info(
                "agent_cache_hit run=%s agent=%s source_run=%s",
                run_id,
                research_planner.SPEC.name,
                cache_run_id,
            )
        else:
            cache_run_id = None
            topics = _research_topics(
                planner_input, current_source_ids, model_config, store, run_id
            )

        cached_research = store.reusable_artifact(
            *cache_arguments, researcher.SPEC.name
        )
        if cached_research and cached_research[0] == cache_run_id:
            research_markdown = str(cached_research[1].get("markdown", "")).strip()
        else:
            research_markdown = ""
        if research_markdown:
            store.save_artifact(
                run_id,
                researcher.SPEC.name,
                {
                    **cached_research[1],
                    "_cache": {"hit": True, "source_run_id": cache_run_id},
                },
            )
            logger.info(
                "agent_cache_hit run=%s agent=%s source_run=%s",
                run_id,
                researcher.SPEC.name,
                cache_run_id,
            )
        else:
            cached_search = store.reusable_artifact(
                *cache_arguments, "research_search"
            )
            research_markdown = _research_section(
                topics,
                information_leads,
                current_source_ids,
                model_config,
                store,
                run_id,
                cached_search,
            )

        body = (
            "## 一、整理与回顾\n\n"
            + retrospective_markdown
            + "\n\n## 二、领域探索与研究\n\n"
            + research_markdown
        )
        origin_label = "手动" if origin == "manual" else "自动"
        trigger_label = {
            "manual": "手动生成",
            "scheduled": "系统调度",
            "retry": "自动任务重试",
        }[trigger]
        final_content = (
            f"# {report_name}\n\n"
            f"> 生成时间：{datetime.datetime.now():%Y-%m-%d %H:%M}\n"
            f"> 报告来源：{origin_label}\n"
            f"> 触发方式：{trigger_label}\n"
            f"> 原始日记范围：{start:%Y-%m-%d} 至 {end:%Y-%m-%d}\n"
            f"> 分析运行：{run_id}\n\n"
            + body
            + "\n\n"
            + _source_appendix(body, store)
            + "\n"
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = report_path.with_suffix(report_path.suffix + f".{run_id}.tmp")
        previous_content = report_path.read_bytes() if report_path.exists() else None
        temp_path.write_text(final_content, encoding="utf-8")
        temp_path.replace(report_path)
        profile_snapshot: bytes | None = None
        profile_committed = False
        try:
            _, profile_snapshot = profile_store.commit_entries(
                run_id=run_id,
                period_start=start.isoformat(),
                period_end=end.isoformat(),
                entries=entries,
                decisions=decisions,
            )
            profile_committed = True
            store.complete_run(run_id, report_path)
        except Exception:
            if profile_committed:
                profile_store.restore(profile_snapshot)
            if previous_content is None:
                report_path.unlink(missing_ok=True)
            else:
                restore = report_path.with_suffix(report_path.suffix + f".{run_id}.restore.tmp")
                restore.write_bytes(previous_content)
                restore.replace(report_path)
            raise
        logger.info("analysis_completed run=%s kind=%s", run_id, kind)
        return body, True, report_path
    except Exception as error:
        message = str(error) or error.__class__.__name__
        if store is not None and run_id is not None:
            try:
                store.fail_run(run_id, message)
            except Exception as state_error:
                message += f"；保存失败状态时又发生异常: {state_error}"
        logger.error(
            "analysis_failed run=%s error_type=%s",
            run_id or "not-started",
            error.__class__.__name__,
        )
        return f"分析失败: {message}", False, None
    finally:
        report_lock.release()
