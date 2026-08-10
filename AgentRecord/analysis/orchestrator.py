"""Orchestrate weekly research reports and summary-only monthly reports."""

import datetime
import hashlib
import json
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .. import journal, settings
from ..agents import researcher, research_planner, retrospective, reviewer
from ..agents.base import AgentPipelineError, invoke_agent
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
    _log_without_summary,
    _monthly_supporting_reports,
    _period_records,
    _recent_summary_context,
    _record_chunks,
    _referenced_source_records,
)


logger = logging.getLogger(__name__)
_MAX_AGENT_INPUT_CHARACTERS = 120000
_MAX_RECORD_CHUNK_CHARACTERS = 30000
_MAX_PLANNER_RECORD_CHARACTERS = 90000
_NO_PRIVATE_RESEARCH_TOPIC = (
    "本周记录没有产生适合公开检索且不泄露隐私的探索主题。"
)
_NO_SUPPORTED_RESEARCH = "本周候选主题未获得足够公开证据。"


@dataclass
class UsageAccumulator:
    """Accumulate only model calls made by the current report process."""

    usage: dict[str, int] = field(
        default_factory=lambda: {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "cache_miss_tokens": 0,
        }
    )

    def observe(self, telemetry: dict | None) -> None:
        values = telemetry.get("usage", {}) if isinstance(telemetry, dict) else {}
        for key in self.usage:
            self.usage[key] += int(values.get(key, 0) or 0)

    def totals(self) -> dict[str, int]:
        return dict(self.usage)


def summarize_diary(date: str, model_config: settings.ModelDict) -> tuple[str, bool]:
    """Generate and safely store one non-thinking daily summary."""
    file_path = settings.DIARY_DIR / f"{date}.md"
    if not file_path.exists():
        return f"找不到 {date} 的记录。", False
    original_content = file_path.read_text(encoding="utf-8")
    original_hash = hashlib.sha256(original_content.encode("utf-8")).hexdigest()
    content = _log_without_summary(original_content)
    prompt = f"""[程序日记总结任务]
请总结 {date} 的日记。只输出要写入 <summary> 的 Markdown 正文，不要输出标题、标签、代码围栏或完成提示。

要求：
- 在完整保留重要信息的前提下简洁概括当天的事件、观点、决定、问题和进展，不逐条复述，不重复或无必要展开。
- 区分用户记录与引用的 AI 内容；AI 内容不能当作用户已经认可的观点。
- 保留重要具体信息，禁止编造、心理诊断和行为指导。

【{date} 原始日记】
{content}"""
    response = call_ai(
        prompt,
        model_config,
        thinking=False,
        max_tokens=4096,
    )
    summary, success = response
    if not success:
        return summary, False
    summary = summary.strip()
    if not summary or summary == "(AI 未给出最终回答)":
        return "日记总结为空。", False
    if (
        "```" in summary
        or re.search(r"</?summary>", summary, re.IGNORECASE)
        or re.search(r"(?m)^\s{0,3}#{1,6}\s+", summary)
    ):
        return "日记总结包含标题、代码围栏或 summary 包装，未写入。", False
    result = journal.update_summary_for_date(
        date,
        summary,
        expected_content_hash=original_hash,
    )
    if not result.endswith("总结已写入文档顶部。"):
        return result, False
    return summary, True


def _revision_context(
    attempt: int,
    previous_output: object,
    feedback: object,
    *,
    source: str,
    maximum_attempts: int | None = None,
) -> dict:
    """Build a bounded correction suffix without internal telemetry."""
    if maximum_attempts is None:
        maximum_attempts = settings.retry_policy()["agent_revision_limit"] + 1

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


def _call_agent(
    spec,
    task: str,
    input_data: dict,
    model_config: settings.ModelDict,
    usage: UsageAccumulator,
    run_id: str,
    *,
    revision_context: dict | None = None,
) -> tuple[dict | str, dict]:
    logger.info("agent_start run=%s agent=%s", run_id, spec.name)
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
    try:
        result, telemetry = invoke_agent(
            spec,
            task,
            input_data,
            model_config,
            call_ai,
            revision_context=revision_context,
        )
    except AgentPipelineError as error:
        usage.observe(error.telemetry)
        logger.warning(
            "agent_failed run=%s agent=%s error_type=%s",
            run_id,
            spec.name,
            error.__class__.__name__,
        )
        raise
    usage.observe(telemetry)
    logger.info(
        "agent_completed run=%s agent=%s duration_ms=%s total_tokens=%s "
        "cached_tokens=%s cache_miss_tokens=%s",
        run_id,
        spec.name,
        telemetry.get("duration_ms", 0),
        telemetry.get("usage", {}).get("total_tokens", 0),
        telemetry.get("usage", {}).get("cached_tokens", 0),
        telemetry.get("usage", {}).get("cache_miss_tokens", 0),
    )
    return result, telemetry


def _review_body(
    mode: str,
    text: str,
    review_context: dict,
    model_config: settings.ModelDict,
    usage: UsageAccumulator,
    run_id: str,
) -> tuple[bool, str, dict]:
    """Review once, allowing one Reviewer-only schema correction."""
    review_input = {
        "mode": mode,
        "text": text,
        "materials": review_context,
    }
    revision_context = None
    for attempt in range(2):
        payload, telemetry = _call_agent(
            reviewer.SPEC,
            "审查这一份正文，并按最小对象返回结论和一段修改意见。",
            review_input,
            model_config,
            usage,
            run_id,
            revision_context=revision_context,
        )
        if not isinstance(payload, dict):
            raise AgentPipelineError("Reviewer 未返回 JSON 对象")
        try:
            passed, feedback = reviewer.validate(payload)
        except AgentPipelineError:
            if attempt or int(telemetry.get("protocol_retries", 0) or 0):
                raise
            revision_context = _revision_context(
                2,
                payload,
                "修正 approved/feedback 字段、类型和一致性",
                source="Reviewer 协议校验",
                maximum_attempts=2,
            )
            continue
        return passed, feedback, payload
    raise RuntimeError("unreachable")


def _model_record(record: dict) -> dict:
    """Expose semantic fields to models, never controller fingerprints or paths."""
    visible = {
        key: record[key]
        for key in ("date", "time", "tag", "speaker", "text", "text_part")
        if key in record
    }
    return visible


def _retrospective_input(
    period: dict,
    current_records: list[dict],
    referenced_records: list[dict],
    recent_summaries: str,
    weekly_retrospectives: str,
    *,
    chunk: dict | None = None,
) -> dict:
    value = {
        "period": period,
        "facts": {
            "current_records": [_model_record(item) for item in current_records],
            "referenced_records": [
                _model_record(item) for item in referenced_records
            ],
        },
        "context": {
            "recent_summaries": recent_summaries,
            "weekly_retrospectives": weekly_retrospectives,
        },
        "trust_boundaries": {
            "facts.current_records": "当期原始记录，是当期事实的主要依据",
            "facts.referenced_records": "用户显式引用的历史记录",
            "context.recent_summaries": "派生辅助上下文，不能单独证明当期事实",
            "context.weekly_retrospectives": "派生辅助上下文，只帮助月报整理",
        },
    }
    if chunk:
        value["chunk"] = chunk
    return value


def _record_dates(records: list[dict]) -> list[str]:
    return list(dict.fromkeys(str(record.get("date", "")) for record in records))


def _record_basis(records: list[dict]) -> str:
    dates = [date for date in _record_dates(records) if date]
    return "> 记录依据：" + (", ".join(dates) if dates else "无可显示日期")


def _retrospective_section(
    base_input: dict,
    source_records: list[dict],
    model_config: settings.ModelDict,
    usage: UsageAccumulator,
    run_id: str,
    *,
    task: str = "生成整理与回顾板块。",
) -> str:
    revision_limit = settings.retry_policy()["agent_revision_limit"]
    revision_context = None
    last_feedback = ""
    for attempt in range(1, revision_limit + 2):
        result, _ = _call_agent(
            retrospective.SPEC,
            task,
            base_input,
            model_config,
            usage,
            run_id,
            revision_context=revision_context,
        )
        try:
            body = retrospective.validate(result)
        except AgentPipelineError as error:
            logger.warning(
                "agent_validation_failed run=%s agent=%s reason=%s",
                run_id,
                retrospective.SPEC.name,
                str(error),
            )
            if attempt > revision_limit:
                raise
            revision_context = _revision_context(
                attempt + 1,
                result,
                str(error),
                source="中控确定性校验",
                maximum_attempts=revision_limit + 1,
            )
            continue

        passed, last_feedback, _ = _review_body(
            "retrospective_review",
            body,
            base_input,
            model_config,
            usage,
            run_id,
        )
        if passed:
            return body + "\n\n" + _record_basis(source_records)
        if attempt > revision_limit:
            raise AgentPipelineError("整理与回顾未通过审查: " + last_feedback)
        revision_context = _revision_context(
            attempt + 1,
            body,
            last_feedback,
            source="Reviewer 实质审查",
            maximum_attempts=revision_limit + 1,
        )
    raise AgentPipelineError("整理与回顾修订次数耗尽: " + last_feedback)


def _retrospective_with_input_budget(
    period: dict,
    current_records: list[dict],
    referenced_records: list[dict],
    recent_summaries: str,
    weekly_retrospectives: str,
    model_config: settings.ModelDict,
    usage: UsageAccumulator,
    run_id: str,
) -> str:
    base_input = _retrospective_input(
        period,
        current_records,
        referenced_records,
        recent_summaries,
        weekly_retrospectives,
    )
    source_records = [*current_records, *referenced_records]
    if len(json.dumps(base_input, ensure_ascii=False)) <= _MAX_AGENT_INPUT_CHARACTERS:
        return _retrospective_section(
            base_input, source_records, model_config, usage, run_id
        )

    fixed_input = _retrospective_input(
        period, [], [], recent_summaries, weekly_retrospectives
    )
    if len(json.dumps(fixed_input, ensure_ascii=False)) >= _MAX_AGENT_INPUT_CHARACTERS:
        raise AgentPipelineError("Retrospective 固定辅助上下文超过安全上限")
    chunks = _record_chunks(source_records, _MAX_RECORD_CHUNK_CHARACTERS)
    current_source_ids = {record["source_id"] for record in current_records}
    sections = []
    for index, chunk_records in enumerate(chunks, 1):
        chunk_current = [
            item for item in chunk_records if item["source_id"] in current_source_ids
        ]
        chunk_referenced = [
            item for item in chunk_records if item["source_id"] not in current_source_ids
        ]
        chunk_input = _retrospective_input(
            period,
            chunk_current,
            chunk_referenced,
            recent_summaries,
            weekly_retrospectives,
            chunk={"index": index, "total": len(chunks)},
        )
        sections.append(
            _retrospective_section(
                chunk_input,
                chunk_records,
                model_config,
                usage,
                run_id,
                task=(
                    "生成整理与回顾板块。"
                    f"当前只处理第 {index}/{len(chunks)} 个原文分块；"
                    "不得声称覆盖未提供的分块。"
                ),
            )
        )
    return "\n\n".join(sections)


def _planner_record_groups(
    records_by_date: dict[str, list[dict]], maximum_groups: int = 5
) -> list[dict]:
    """Balance consecutive records by serialized size, with bounded model input."""
    units = []
    for date in sorted(records_by_date):
        records = records_by_date[date]
        size = len(json.dumps(records, ensure_ascii=False))
        if size <= _MAX_PLANNER_RECORD_CHARACTERS:
            units.append({"dates": [date], "records": records, "size": size})
            continue
        for chunk in _record_chunks(records, _MAX_PLANNER_RECORD_CHARACTERS):
            units.append(
                {
                    "dates": [date],
                    "records": chunk,
                    "size": len(json.dumps(chunk, ensure_ascii=False)),
                }
            )
    if not units:
        return []
    if len(units) > maximum_groups:
        total_size = sum(unit["size"] for unit in units)
        if total_size > maximum_groups * _MAX_PLANNER_RECORD_CHARACTERS:
            raise AgentPipelineError("全周 Planner 输入超过五组安全容量")

    group_count = min(maximum_groups, len(units))
    groups = []
    current_units = []
    current_size = 0
    remaining_size = sum(unit["size"] for unit in units)
    for index, unit in enumerate(units):
        remaining_groups = group_count - len(groups)
        remaining_units = len(units) - index
        target = math.ceil(remaining_size / max(1, remaining_groups))
        if (
            current_units
            and len(groups) < group_count - 1
            and current_size + unit["size"] > target
            and remaining_units >= remaining_groups - 1
        ):
            groups.append(current_units)
            remaining_size -= current_size
            current_units = []
            current_size = 0
        current_units.append(unit)
        current_size += unit["size"]
    if current_units:
        groups.append(current_units)

    result = []
    for grouped_units in groups:
        records = [record for unit in grouped_units for record in unit["records"]]
        if len(json.dumps(records, ensure_ascii=False)) > _MAX_AGENT_INPUT_CHARACTERS:
            raise AgentPipelineError("单个 Planner 记录组超过输入安全上限")
        result.append(
            {
                "dates": list(
                    dict.fromkeys(
                        date for unit in grouped_units for date in unit["dates"]
                    )
                ),
                "records": records,
            }
        )
    return result


def _research_topics(
    period: dict,
    records: list[dict],
    model_config: settings.ModelDict,
    usage: UsageAccumulator,
    run_id: str,
) -> list[dict]:
    records_by_date: dict[str, list[dict]] = {}
    for record in records:
        records_by_date.setdefault(str(record.get("date", "")), []).append(record)
    topics = []
    seen_queries = set()
    revision_limit = settings.retry_policy()["agent_revision_limit"]
    for group in _planner_record_groups(records_by_date):
        model_group = {
            "dates": group["dates"],
            "records": [_model_record(record) for record in group["records"]],
        }
        revision_context = None
        query = None
        for attempt in range(1, revision_limit + 2):
            payload, _ = _call_agent(
                research_planner.SPEC,
                "为这一组记录选择一个公开研究问题；没有合适问题就返回 skip。",
                {
                    "period": period,
                    "record_group": model_group,
                    "already_selected_queries": [topic["query"] for topic in topics],
                },
                model_config,
                usage,
                run_id,
                revision_context=revision_context,
            )
            if not isinstance(payload, dict):
                raise AgentPipelineError("ResearchPlanner 未返回 JSON 对象")
            try:
                query = research_planner.normalize_query(payload)
            except AgentPipelineError as error:
                logger.warning(
                    "agent_validation_failed run=%s agent=%s reason=%s",
                    run_id,
                    research_planner.SPEC.name,
                    str(error),
                )
                if attempt > revision_limit:
                    raise
                revision_context = _revision_context(
                    attempt + 1,
                    payload,
                    str(error),
                    source="中控确定性校验",
                    maximum_attempts=revision_limit + 1,
                )
                continue
            break
        if not query or query.casefold() in seen_queries:
            continue
        topic_id = f"Q{len(topics) + 1:03d}"
        topics.append(
            {
                "topic_id": topic_id,
                "query": query,
                "record_dates": group["dates"],
            }
        )
        seen_queries.add(query.casefold())
    return topics


def _collect_research_evidence(
    topics: list[dict], run_id: str
) -> tuple[list[dict], list[dict]]:
    """Search each fixed query once and assign controller-owned evidence IDs."""
    evidence = []
    usable_topics = []
    search_results = 0
    for topic in topics:
        result, error = search_web_once(topic["query"])
        if error:
            raise AgentPipelineError(error)
        search_results += result.result_count
        topic_evidence = []
        seen_urls = set()
        for item in result.evidence:
            url = str(item.get("url", "")).strip()
            try:
                url_key = researcher.canonical_url(url)
            except ValueError:
                continue
            if (
                len(url) > 4096
                or re.search(r"[\x00-\x20\x7f]", url)
                or url_key[0] not in {"http", "https"}
                or not url_key[1]
                or url_key in seen_urls
            ):
                continue
            seen_urls.add(url_key)
            topic_evidence.append(
                {
                    "source_id": (
                        f"W-{topic['topic_id']}-{len(topic_evidence) + 1:03d}"
                    ),
                    "topic_id": topic["topic_id"],
                    "title": str(item.get("title", ""))[:300],
                    "url": url,
                    "snippet": str(item.get("snippet", ""))[:800],
                    "published": str(item.get("published", ""))[:80],
                }
            )
        if topic_evidence:
            usable_topics.append(topic)
            evidence.extend(topic_evidence)
    logger.info(
        "research_search_completed run=%s queries=%s results=%s usable_topics=%s",
        run_id,
        len(topics),
        search_results,
        len(usable_topics),
    )
    return usable_topics, evidence


def _research_one_topic(
    topic: dict,
    evidence: list[dict],
    model_config: settings.ModelDict,
    usage: UsageAccumulator,
    run_id: str,
) -> dict:
    topic_evidence = [
        item for item in evidence if item["topic_id"] == topic["topic_id"]
    ]
    research_input = {
        "question": topic["query"],
        "record_dates": topic["record_dates"],
        "evidence": {
            "search_results": [
                {
                    "title": item["title"],
                    "snippet": item["snippet"],
                    "published": item["published"],
                }
                for item in topic_evidence
            ]
        },
        "trust_boundaries": {
            "evidence.search_results": "外部不可信资料，只用于当前研究主题"
        },
    }
    revision_context = None
    last_feedback = ""
    revision_limit = settings.retry_policy()["agent_revision_limit"]
    for attempt in range(1, revision_limit + 2):
        payload, _ = _call_agent(
            researcher.SPEC,
            "研究这一个问题，并按最小对象返回状态和正文。",
            research_input,
            model_config,
            usage,
            run_id,
            revision_context=revision_context,
        )
        if not isinstance(payload, dict):
            raise AgentPipelineError("Researcher 未返回 JSON 对象")
        try:
            status, body = researcher.validate(payload)
            if status == "insufficient":
                return {"topic": topic, "accepted": False, "feedback": body}
            markdown = researcher.render_topic(body, topic, topic_evidence)
        except AgentPipelineError as error:
            logger.warning(
                "agent_validation_failed run=%s agent=%s reason=%s",
                run_id,
                researcher.SPEC.name,
                str(error),
            )
            if attempt > revision_limit:
                return {"topic": topic, "accepted": False, "feedback": str(error)}
            revision_context = _revision_context(
                attempt + 1,
                payload,
                str(error),
                source="中控确定性校验",
                maximum_attempts=revision_limit + 1,
            )
            continue

        passed, last_feedback, _ = _review_body(
            "research_review",
            body,
            research_input,
            model_config,
            usage,
            run_id,
        )
        if passed:
            return {
                "topic": topic,
                "accepted": True,
                "markdown": markdown,
            }
        if attempt > revision_limit:
            return {
                "topic": topic,
                "accepted": False,
                "feedback": last_feedback,
            }
        revision_context = _revision_context(
            attempt + 1,
            body,
            last_feedback,
            source="Reviewer 实质审查",
            maximum_attempts=revision_limit + 1,
        )
    raise RuntimeError("unreachable")


def _grounded_research_section(
    topics: list[dict],
    model_config: settings.ModelDict,
    usage: UsageAccumulator,
    run_id: str,
) -> str:
    if not topics:
        return _NO_PRIVATE_RESEARCH_TOPIC
    usable_topics, evidence = _collect_research_evidence(topics, run_id)
    if not usable_topics:
        return _NO_SUPPORTED_RESEARCH
    topic_results = [
        _research_one_topic(topic, evidence, model_config, usage, run_id)
        for topic in usable_topics
    ]
    accepted = [result for result in topic_results if result["accepted"]]
    if not accepted:
        return _NO_SUPPORTED_RESEARCH
    if len(accepted) != len(topic_results):
        logger.info(
            "research_topics_dropped run=%s accepted=%s dropped=%s",
            run_id,
            len(accepted),
            len(topic_results) - len(accepted),
        )
    return "\n\n".join(result["markdown"] for result in accepted)


def _model_label(model_config: settings.ModelDict) -> str:
    model_id = str(model_config.get("model_id", "")).strip()
    name = str(model_config.get("name", "")).strip()
    return model_id or name or "未标明"


def _duration_label(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    total_seconds = round(seconds)
    minutes, remaining = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分 {remaining} 秒"
    return f"{minutes} 分 {remaining} 秒"


def _token_label(usage: dict[str, int]) -> str:
    prompt = usage.get("prompt_tokens", 0)
    completion = usage.get("completion_tokens", 0)
    total = usage.get("total_tokens", 0) or prompt + completion
    cached = usage.get("cached_tokens", 0)
    cache_miss = usage.get("cache_miss_tokens", 0)
    return (
        f"{total:,}（输入 {prompt:,}，输出 {completion:,}，"
        f"缓存命中 {cached:,}，缓存未命中 {cache_miss:,}）"
    )


def generate_analysis_report(
    kind: str,
    anchor: datetime.date,
    model_config: settings.ModelDict,
    *,
    origin: str = "manual",
    trigger: str | None = None,
) -> tuple[str, bool, Path | None]:
    """Generate one report from a frozen input snapshot and in-memory state."""
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
    if trigger not in {"manual", "scheduled", "retry"}:
        return f"未知触发方式: {trigger}", False, None
    if kind == "weekly" and not third_party_search_available():
        return (
            f"{CONFIG_ERROR_MARKER} 周报需要启用第三方搜索，"
            "以便中控逐条执行查询并审计来源。",
            False,
            None,
        )

    report_path = _analysis_report_path(kind, start, end, origin)
    report_lock = FileLock.acquire(settings.ANALYSIS_DIR / ".report.lock")
    if report_lock is None:
        return "另一个分析报告正在生成，请稍后重试。", False, None
    generation_started = time.perf_counter()
    run_id = uuid.uuid4().hex
    temp_path: Path | None = None
    usage = UsageAccumulator()
    try:
        period = {
            "kind": kind,
            "start": start.isoformat(),
            "end": end.isoformat(),
        }
        logger.info(
            "analysis_started run=%s kind=%s origin=%s trigger=%s period=%s..%s",
            run_id,
            kind,
            origin,
            trigger,
            start,
            end,
        )
        logs = _existing_logs(start, end)
        if not logs:
            return (
                f"{start:%Y-%m-%d} 至 {end:%Y-%m-%d} 没有日记记录。",
                False,
                None,
            )
        records = _period_records(logs)
        if not records:
            return "日记中没有可识别的标准记录。", False, None
        referenced_records = _referenced_source_records(logs)
        recent_summaries = _recent_summary_context(start)
        weekly_retrospectives = (
            _monthly_supporting_reports(start, end)
            if kind == "monthly"
            else "（周报不读取下级周期报告）"
        )
        retrospective_markdown = _retrospective_with_input_budget(
            period,
            records,
            referenced_records,
            recent_summaries,
            weekly_retrospectives,
            model_config,
            usage,
            run_id,
        )
        if kind == "weekly":
            topics = _research_topics(period, records, model_config, usage, run_id)
            research_markdown = _grounded_research_section(
                topics, model_config, usage, run_id
            )
            body = (
                "## 一、整理与回顾\n\n"
                + retrospective_markdown
                + "\n\n## 二、领域探索与研究\n\n"
                + research_markdown
            )
        else:
            body = "## 整理与回顾\n\n" + retrospective_markdown

        origin_label = "手动" if origin == "manual" else "自动"
        trigger_label = {
            "manual": "手动生成",
            "scheduled": "系统调度",
            "retry": "自动任务重试",
        }[trigger]
        final_content = (
            f"# {report_name}\n\n"
            f"> 生成时间：{datetime.datetime.now():%Y-%m-%d %H:%M}\n"
            f"> 使用模型：{_model_label(model_config)}\n"
            f"> 生成耗时：{_duration_label(time.perf_counter() - generation_started)}\n"
            f"> Token 用量：{_token_label(usage.totals())}\n"
            f"> 报告来源：{origin_label}\n"
            f"> 触发方式：{trigger_label}\n"
            f"> 原始日记范围：{start:%Y-%m-%d} 至 {end:%Y-%m-%d}\n"
            f"> 分析运行：{run_id}\n\n"
            + body
            + "\n"
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = report_path.with_suffix(report_path.suffix + f".{run_id}.tmp")
        temp_path.write_text(final_content, encoding="utf-8")
        temp_path.replace(report_path)
        temp_path = None
        logger.info("analysis_completed run=%s kind=%s", run_id, kind)
        return body, True, report_path
    except Exception as error:
        message = str(error) or error.__class__.__name__
        logger.error(
            "analysis_failed run=%s error_type=%s",
            run_id,
            error.__class__.__name__,
        )
        return f"分析失败: {message}", False, None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("analysis_temp_cleanup_failed run=%s", run_id)
        report_lock.release()
