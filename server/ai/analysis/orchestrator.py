"""单次 Report Agent 直接生成完整周报 / 月报。"""

import datetime
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .. import journal, settings
from ..agents import (
    REPORT_SPEC,
    AgentPipelineError,
    invoke_agent,
    is_json_container,
)
from ..ai_client import (
    CONFIG_ERROR_MARKER,
    call_ai,
)
from ..file_lock import FileLock
from .context import (
    _analysis_report_path,
    _existing_logs,
    _log_without_summary,
    _period_records,
    _period_span,
)


logger = logging.getLogger(__name__)
_MAX_AGENT_INPUT_CHARACTERS = 120000


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


def _normalize_daily_summary(value: object) -> tuple[str, list[str]]:
    """Remove harmless outer presentation wrappers, then validate the body."""
    if not isinstance(value, str):
        return "", ["总结不是文本"]
    summary = value.strip()
    fenced = re.fullmatch(
        r"```(?:markdown|md|text)?\s*\n?(.*?)\n?```",
        summary,
        re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        summary = fenced.group(1).strip()
    wrapped = re.fullmatch(
        r"<summary>\s*(.*?)\s*</summary>",
        summary,
        re.DOTALL | re.IGNORECASE,
    )
    if wrapped:
        summary = wrapped.group(1).strip()
    lines = summary.splitlines()
    if lines and re.fullmatch(r"\s{0,3}#{1,6}\s+.+", lines[0]):
        summary = "\n".join(lines[1:]).strip()

    errors = []
    if not summary or summary == "(AI 未给出最终回答)":
        errors.append("总结为空")
    if is_json_container(summary):
        errors.append("总结输出了 JSON")
    if "```" in summary:
        errors.append("总结包含代码围栏")
    if re.search(r"</?summary>", summary, re.IGNORECASE):
        errors.append("总结包含非外层 summary 标签")
    if re.search(r"(?m)^\s{0,3}#{1,6}\s+", summary):
        errors.append("总结包含正文内标题")
    return summary, errors


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
- 用**短要点分点**呈现（Markdown 无序列表），每条一个独立要点，避免写成一大段连续文字，让排版清晰、阅读舒服。
- 可用加粗主题词作为要点开头引导（例如 **工作**、**健康**、**决定**、**问题**、**进展**），按当天实际情况灵活分组。
- 在完整保留重要信息的前提下简洁概括当天的事件、观点、决定、问题和进展，不逐条复述，不重复或无必要展开。
- 区分用户记录与引用的 AI 内容；AI 内容不能当作用户已经认可的观点。
- 保留重要具体信息，禁止编造、心理诊断和行为指导。

【{date} 原始日记】
{content}"""
    try:
        maximum_attempts = settings.retry_policy()["daily_summary_retry_limit"] + 1
    except RuntimeError as error:
        return f"{CONFIG_ERROR_MARKER} {error}", False
    current_prompt = prompt
    summary = ""
    for attempt in range(1, maximum_attempts + 1):
        response = call_ai(
            current_prompt,
            model_config,
            thinking=False,
            max_tokens=4096,
        )
        raw_summary, success = response
        if not success:
            return raw_summary, False
        summary, errors = _normalize_daily_summary(raw_summary)
        if not errors:
            break
        problem = "；".join(errors)
        logger.warning(
            "daily_summary_validation_failed date=%s attempt=%s problems=%s",
            date,
            attempt,
            problem,
        )
        if attempt == maximum_attempts:
            return (
                f"日记总结连续 {maximum_attempts} 次未通过校验: {problem}",
                False,
            )
        current_prompt = (
            prompt
            + "\n\n【中控格式修正】\n"
            + f"上一响应未通过格式校验：{problem}。"
            + "请重新执行原任务，只输出连续的 Markdown 正文。"
        )
    result = journal.update_summary_for_date(
        date,
        summary,
        expected_content_hash=original_hash,
    )
    if not result.endswith("总结已写入文档顶部。"):
        return result, False
    return summary, True


def _call_report_agent(
    task: str,
    input_data: dict,
    model_config: settings.ModelDict,
    usage: UsageAccumulator,
    run_id: str,
) -> str:
    logger.info("agent_start run=%s agent=%s", run_id, REPORT_SPEC.name)
    input_size = len(json.dumps(input_data, ensure_ascii=False))
    if input_size > _MAX_AGENT_INPUT_CHARACTERS:
        raise AgentPipelineError(
            f"{REPORT_SPEC.name} 输入超过安全上限（{input_size} > "
            f"{_MAX_AGENT_INPUT_CHARACTERS} 字符）"
        )
    try:
        body, telemetry = invoke_agent(
            REPORT_SPEC,
            task,
            input_data,
            model_config,
            call_ai,
        )
    except AgentPipelineError as error:
        usage.observe(error.telemetry)
        logger.warning(
            "agent_failed run=%s agent=%s error_type=%s",
            run_id,
            REPORT_SPEC.name,
            error.__class__.__name__,
        )
        raise
    usage.observe(telemetry)
    logger.info(
        "agent_completed run=%s agent=%s duration_ms=%s total_tokens=%s "
        "cached_tokens=%s cache_miss_tokens=%s",
        run_id,
        REPORT_SPEC.name,
        telemetry.get("duration_ms", 0),
        telemetry.get("usage", {}).get("total_tokens", 0),
        telemetry.get("usage", {}).get("cached_tokens", 0),
        telemetry.get("usage", {}).get("cache_miss_tokens", 0),
    )
    return body


def _report_input(period: dict, records: list[dict]) -> dict:
    """按日期分隔 + 行号标注 + 全局引用编号，作为唯一事实源交给 Report Agent。"""
    labeled = []
    for index, record in enumerate(records, 1):
        labeled.append(
            {
                "n": index,
                "date": record.get("date", ""),
                "line": record.get("line", 0),
                "time": record.get("time", ""),
                "tag": record.get("tag", ""),
                "text": record.get("text", ""),
            }
        )
    return {"period": period, "records": labeled}


def _report_task(kind: str) -> str:
    if kind == "weekly":
        return (
            "根据中控提供的本周期完整原始记录流，一次性生成整份周报正文。"
            "覆盖本周做了什么、关注点、进展、问题与想法变化，全部以记录流为唯一事实来源。"
        )
    return (
        "根据中控提供的本周期完整原始记录流，一次性生成整份月报正文。"
        "覆盖本月做了什么、关注点、进展、问题与想法变化，全部以记录流为唯一事实来源。"
    )


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
) -> tuple[str, bool, Path | None]:
    """单次 Report Agent 直接生成完整周报 / 月报（手动/自动同一流程、同一路径）。"""
    span = _period_span(kind, anchor)
    if span is None:
        return "分析报告只支持 weekly 或 monthly。", False, None
    start, end = span
    report_name = (
        f"{start:%Y-%m-%d} 至 {end:%Y-%m-%d} 分析周报"
        if kind == "weekly"
        else f"{start:%Y年%m月} 分析月报"
    )

    report_path = _analysis_report_path(kind, start, end)
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
            "analysis_started run=%s kind=%s period=%s..%s",
            run_id,
            kind,
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

        input_data = _report_input(period, records)
        body = _call_report_agent(
            _report_task(kind),
            input_data,
            model_config,
            usage,
            run_id,
        )
        if not body:
            return "报告正文为空。", False, None

        final_content = (
            f"# {report_name}\n\n"
            f"> 生成时间：{datetime.datetime.now():%Y-%m-%d %H:%M}\n"
            f"> 使用模型：{_model_label(model_config)}\n"
            f"> 生成耗时：{_duration_label(time.perf_counter() - generation_started)}\n"
            f"> Token 用量：{_token_label(usage.totals())}\n"
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