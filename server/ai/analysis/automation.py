"""Automatic due-task execution and failure-state tracking (in-process daemon)."""

import datetime
import hashlib
import json
import logging
import uuid
from pathlib import Path

from .. import journal, settings
from ..ai_client import (
    CONFIG_ERROR_MARKER,
    is_config_failure,
    is_network_failure,
    is_rate_limit_failure,
)
from ..file_lock import FileLock
from .context import (
    _analysis_report_path,
    _existing_logs,
    _monthly_supporting_reports,
    _recent_summary_context,
    _referenced_source_records,
)
from .orchestrator import (
    generate_analysis_report,
    summarize_diary,
)


logger = logging.getLogger(__name__)


_FAILURE_SIGNATURE_VERSION = 1
_AUTOMATION_TASK_ORDER = (
    "daily_summary",
    "weekly_report",
    "monthly_report",
)


def _load_automation_state() -> dict:
    state_path = settings.ANALYSIS_DIR / ".automation-state.json"
    if not state_path.exists():
        return {}
    try:
        value = json.loads(state_path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def _save_automation_state(state: dict) -> None:
    settings.ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    state_path = settings.ANALYSIS_DIR / ".automation-state.json"
    temp_path = settings.ANALYSIS_DIR / f".automation-state.{uuid.uuid4().hex}.tmp"
    try:
        temp_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp_path.replace(state_path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _automation_model() -> settings.ModelDict:
    return settings.ModelConfig.get_model()


def _next_content_retry_boundary(now: datetime.datetime) -> datetime.datetime:
    interval_minutes = settings.retry_policy()[
        "automation_content_retry_interval_minutes"
    ]
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed_minutes = (now - midnight).total_seconds() / 60
    next_boundary = (
        int(elapsed_minutes // interval_minutes) + 1
    ) * interval_minutes
    return midnight + datetime.timedelta(minutes=next_boundary)


def _default_task_target(task: str, now: datetime.datetime) -> dict[str, str]:
    today = now.date()
    if task == "daily_summary":
        date = today - datetime.timedelta(days=1)
        return {"start": date.isoformat(), "end": date.isoformat()}
    if task == "weekly_report":
        start, end = _latest_week_period(today)
        return {"start": start.isoformat(), "end": end.isoformat()}
    if task == "monthly_report":
        start, end = _latest_month_period(today)
        return {"start": start.isoformat(), "end": end.isoformat()}
    return {}


def _normalized_task_target(
    task: str, value: object
) -> dict[str, str] | None:
    if not isinstance(value, dict) or not value.get("start") or not value.get("end"):
        return None
    target = {"start": str(value["start"]), "end": str(value["end"])}
    try:
        start = datetime.date.fromisoformat(target["start"])
        end = datetime.date.fromisoformat(target["end"])
    except ValueError:
        return None
    if task == "daily_summary":
        valid = start == end
    elif task == "weekly_report":
        valid = start.weekday() == 0 and end == start + datetime.timedelta(days=6)
    elif task == "monthly_report":
        next_month = (start.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        valid = start.day == 1 and end == next_month - datetime.timedelta(days=1)
    else:
        valid = False
    return target if valid else None


def _stored_task_target(
    state: dict, task: str, now: datetime.datetime
) -> dict[str, str]:
    target = _normalized_task_target(
        task, state.get("failure_targets", {}).get(task)
    )
    return target or _default_task_target(task, now)


def _pending_task_targets(state: dict, task: str) -> list[dict[str, str]]:
    """Return validated pending targets in deterministic chronological order."""
    raw_targets = state.get("pending_targets", {}).get(task, [])
    if not isinstance(raw_targets, list):
        return []
    targets = []
    seen = set()
    for value in raw_targets:
        target = _normalized_task_target(task, value)
        if target is None:
            continue
        key = (target["start"], target["end"])
        if key in seen:
            continue
        targets.append(target)
        seen.add(key)
    return sorted(targets, key=lambda item: (item["start"], item["end"]))


def _enqueue_task_target(state: dict, task: str, target: dict[str, str]) -> None:
    targets = _pending_task_targets(state, task)
    normalized = _normalized_task_target(task, target)
    if normalized is None:
        raise ValueError(f"{task} 自动任务目标无效")
    if normalized not in targets:
        targets.append(normalized)
        targets.sort(key=lambda item: (item["start"], item["end"]))
    state.setdefault("pending_targets", {})[task] = targets


def _dequeue_task_target(state: dict, task: str, target: dict[str, str]) -> None:
    targets = [
        item for item in _pending_task_targets(state, task) if item != target
    ]
    pending = state.get("pending_targets", {})
    if targets:
        pending[task] = targets
    else:
        pending.pop(task, None)
    if not pending:
        state.pop("pending_targets", None)


def _clear_pending_task(state: dict, task: str) -> None:
    pending = state.get("pending_targets", {})
    pending.pop(task, None)
    if not pending:
        state.pop("pending_targets", None)


def _has_pending_targets(state: dict) -> bool:
    return any(_pending_task_targets(state, task) for task in _AUTOMATION_TASK_ORDER)


def _secret_digest(value: object) -> str:
    secret = str(value or "").strip()
    return hashlib.sha256(secret.encode("utf-8")).hexdigest() if secret else ""


def _content_failure_key(
    task: str,
    now: datetime.datetime,
    *,
    target: dict[str, str] | None = None,
) -> str:
    """Hash the effective task input without persisting private content or keys."""
    try:
        model = _automation_model()
        model_signature = {
            key: model.get(key)
            for key in (
                "name",
                "model_id",
                "api_url",
                "json_mode",
                "temperature",
            )
        }
        model_signature["api_key_digest"] = _secret_digest(model.get("api_key"))
        target = target or _default_task_target(task, now)
        payload: dict = {
            "failure_signature_version": _FAILURE_SIGNATURE_VERSION,
            "task": task,
            "model": model_signature,
            "retry": settings.retry_policy(),
        }
        if task == "daily_summary":
            date = datetime.date.fromisoformat(target["start"])
            path = settings.DIARY_DIR / f"{date.isoformat()}.md"
            payload.update(
                target=date.isoformat(),
                diary=path.read_text(encoding="utf-8") if path.is_file() else "",
            )
        elif task in {"weekly_report", "monthly_report"}:
            start = datetime.date.fromisoformat(target["start"])
            end = datetime.date.fromisoformat(target["end"])
            if task == "weekly_report":
                supporting_reports = "（周报不读取下级周期报告）"
            else:
                supporting_reports = _monthly_supporting_reports(start, end)
            logs = _existing_logs(start, end)
            payload.update(
                period={"start": start.isoformat(), "end": end.isoformat()},
                logs=logs,
                referenced_sources=_referenced_source_records(logs),
                recent_summaries=_recent_summary_context(start),
                supporting_reports=supporting_reports,
            )
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
    except (OSError, RuntimeError, KeyError, TypeError, ValueError):
        return ""


def _set_task_error(
    state: dict,
    task: str,
    message: str,
    *,
    target: dict[str, str] | None = None,
) -> None:
    now = datetime.datetime.now()
    state.setdefault("errors", {})[task] = f"{now:%Y-%m-%d %H:%M} {message}"
    if task in AUTOMATION_TASK_LABELS:
        target = target or _stored_task_target(state, task, now)
        state.setdefault("failure_targets", {})[task] = target
        _enqueue_task_target(state, task, target)
        if is_config_failure(message):
            state.setdefault("retry_kind", {})[task] = "blocked"
            failure_key = _content_failure_key(task, now, target=target)
            if failure_key:
                state.setdefault("failure_keys", {})[task] = failure_key
            retry_after = state.get("retry_after", {})
            retry_after.pop(task, None)
            if not retry_after:
                state.pop("retry_after", None)
        else:
            network_error = is_network_failure(message)
            rate_limited = is_rate_limit_failure(message)
            if network_error or rate_limited:
                retry_at = now + datetime.timedelta(
                    minutes=settings.retry_policy()[
                        "automation_network_retry_minutes"
                    ]
                )
            else:
                retry_at = _next_content_retry_boundary(now)
            state.setdefault("retry_after", {})[task] = retry_at.isoformat(
                timespec="seconds"
            )
            if network_error:
                retry_kind = "network"
            elif rate_limited:
                retry_kind = "rate_limit"
            else:
                failure_key = _content_failure_key(task, now, target=target)
                previous_key = state.get("failure_keys", {}).get(task)
                try:
                    previous_count = int(
                        state.get("failure_counts", {}).get(task, 0)
                    )
                except (TypeError, ValueError):
                    previous_count = 0
                failure_count = (
                    previous_count + 1
                    if failure_key and failure_key == previous_key
                    else 1
                )
                state.setdefault("failure_counts", {})[task] = failure_count
                if failure_key:
                    state.setdefault("failure_keys", {})[task] = failure_key
                maximum_failures = settings.retry_policy()[
                    "automation_content_failure_limit"
                ]
                if failure_count >= maximum_failures:
                    retry_kind = "content_blocked"
                    state.get("retry_after", {}).pop(task, None)
                    if not state.get("retry_after"):
                        state.pop("retry_after", None)
                else:
                    retry_kind = "content"
            state.setdefault("retry_kind", {})[task] = retry_kind


def _clear_task_error(state: dict, task: str) -> None:
    errors = state.get("errors", {})
    errors.pop(task, None)
    if not errors:
        state.pop("errors", None)
    retry_after = state.get("retry_after", {})
    retry_after.pop(task, None)
    if not retry_after:
        state.pop("retry_after", None)
    retry_kind = state.get("retry_kind", {})
    retry_kind.pop(task, None)
    if not retry_kind:
        state.pop("retry_kind", None)
    failure_counts = state.get("failure_counts", {})
    failure_counts.pop(task, None)
    if not failure_counts:
        state.pop("failure_counts", None)
    failure_keys = state.get("failure_keys", {})
    failure_keys.pop(task, None)
    if not failure_keys:
        state.pop("failure_keys", None)
    failure_targets = state.get("failure_targets", {})
    failure_targets.pop(task, None)
    if not failure_targets:
        state.pop("failure_targets", None)


def _acquire_automation_lock() -> FileLock | None:
    return FileLock.acquire(settings.ANALYSIS_DIR / ".automation.lock")


def _diary_summary_needs_generation(path: Path) -> bool:
    try:
        summary = journal.extract_summary(path.read_text(encoding="utf-8")).strip()
    except OSError:
        return False
    return summary in {"", "(无总结)", "暂无今日总结。"}


def _set_current_task(state: dict, task: str, detail: str) -> None:
    state["current_task"] = task
    state["current_task_detail"] = detail
    state["current_task_started_at"] = datetime.datetime.now().isoformat(
        timespec="seconds"
    )
    _save_automation_state(state)


def _failure_retry_is_due(
    state: dict, task: str, now: datetime.datetime
) -> bool:
    if task not in state.get("errors", {}):
        return True
    if state.get("retry_kind", {}).get(task) in {"blocked", "content_blocked"}:
        return False
    retry_text = str(state.get("retry_after", {}).get(task, ""))
    if retry_text:
        try:
            return now >= datetime.datetime.fromisoformat(retry_text)
        except ValueError:
            return False

    error_text = str(state.get("errors", {}).get(task, ""))
    try:
        failed_at = datetime.datetime.strptime(error_text[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return False
    return now >= _next_content_retry_boundary(failed_at)


def _hour_key(now: datetime.datetime) -> str:
    return now.strftime("%Y-%m-%dT%H")


def _latest_week_period(today: datetime.date) -> tuple[datetime.date, datetime.date]:
    end = today - datetime.timedelta(days=today.weekday() + 1)
    return end - datetime.timedelta(days=6), end


def _latest_month_period(today: datetime.date) -> tuple[datetime.date, datetime.date]:
    end = today.replace(day=1) - datetime.timedelta(days=1)
    return end.replace(day=1), end


def _task_missing(
    task: str,
    now: datetime.datetime,
    *,
    target: dict[str, str] | None = None,
) -> bool:
    target = target or _default_task_target(task, now)
    if task == "daily_summary":
        date = datetime.date.fromisoformat(target["start"])
        path = settings.DIARY_DIR / f"{date.isoformat()}.md"
        return path.exists() and _diary_summary_needs_generation(path)
    if task == "weekly_report":
        start = datetime.date.fromisoformat(target["start"])
        end = datetime.date.fromisoformat(target["end"])
        path = _analysis_report_path("weekly", start, end, "auto")
        return bool(_existing_logs(start, end)) and not path.exists()
    if task == "monthly_report":
        start = datetime.date.fromisoformat(target["start"])
        end = datetime.date.fromisoformat(target["end"])
        path = _analysis_report_path("monthly", start, end, "auto")
        return bool(_existing_logs(start, end)) and not path.exists()
    return False


def _task_artifact_status(task: str, now: datetime.datetime) -> str:
    today = now.date()
    if task == "daily_summary":
        yesterday = today - datetime.timedelta(days=1)
        path = settings.DIARY_DIR / f"{yesterday.isoformat()}.md"
        if not path.exists():
            return f"{yesterday} 无日记"
        return f"{yesterday} {'缺失' if _diary_summary_needs_generation(path) else '已存在'}"
    if task == "weekly_report":
        start, end = _latest_week_period(today)
        if not _existing_logs(start, end):
            return f"{start} 至 {end} 无记录"
        return f"{start} 至 {end} {'缺失' if _task_missing(task, now) else '已存在'}"
    if task == "monthly_report":
        start, end = _latest_month_period(today)
        if not _existing_logs(start, end):
            return f"{start:%Y-%m} 无记录"
        return f"{start:%Y-%m} {'缺失' if _task_missing(task, now) else '已存在'}"
    return "未知"


def _task_should_run(
    state: dict,
    task: str,
    now: datetime.datetime,
    *,
    initial_detection_due: bool,
    target: dict[str, str] | None = None,
) -> bool:
    stored_target = state.get("failure_targets", {}).get(task)
    if target is None and task in state.get("errors", {}) and isinstance(
        stored_target, dict
    ):
        target = _stored_task_target(state, task, now)
    missing = _task_missing(task, now, target=target)
    if not missing:
        _clear_task_error(state, task)
        return False
    if task in state.get("errors", {}):
        retry_kind = state.get("retry_kind", {}).get(task)
        if retry_kind in {"blocked", "content_blocked"}:
            previous_key = str(state.get("failure_keys", {}).get(task, ""))
            current_key = _content_failure_key(task, now, target=target)
            if not current_key or current_key == previous_key:
                return False
            _clear_task_error(state, task)
            if not initial_detection_due:
                return False
        if not _failure_retry_is_due(state, task, now):
            return False
    elif not initial_detection_due:
        return False
    return True


def _run_daily_summaries(
    today: datetime.date,
    state: dict,
    model_config: settings.ModelDict,
    *,
    target: dict[str, str] | None = None,
) -> None:
    date = (
        datetime.date.fromisoformat(target["start"])
        if target
        else today - datetime.timedelta(days=1)
    )
    date_text = date.isoformat()
    path = settings.DIARY_DIR / f"{date_text}.md"
    if not path.exists() or not _diary_summary_needs_generation(path):
        _clear_task_error(state, "daily_summary")
        _save_automation_state(state)
        return
    _set_current_task(state, "daily_summary", f"正在总结 {date_text} 日记")
    message, success = summarize_diary(date_text, model_config)
    if success:
        _clear_task_error(state, "daily_summary")
    else:
        _set_task_error(
            state,
            "daily_summary",
            f"自动总结 {date_text} 失败: {message[:500]}",
            target=target or {"start": date_text, "end": date_text},
        )
    _save_automation_state(state)


def _scan_missing_targets(
    state: dict,
    now: datetime.datetime,
    automation: dict,
    *,
    hourly_detection_due: bool,
) -> None:
    """Persist exact targets before a predecessor can block their execution."""
    for task in _AUTOMATION_TASK_ORDER:
        if automation.get(task, True) is not True:
            _clear_task_error(state, task)
            _clear_pending_task(state, task)
            continue
        if hourly_detection_due:
            target = _default_task_target(task, now)
            if _task_missing(task, now):
                _enqueue_task_target(state, task, target)
            else:
                _dequeue_task_target(state, task, target)
        if task in state.get("errors", {}):
            _enqueue_task_target(state, task, _stored_task_target(state, task, now))


def _run_pending_task(
    task: str,
    target: dict[str, str],
    now: datetime.datetime,
    state: dict,
    model: settings.ModelDict,
    *,
    retry_trigger: bool,
) -> None:
    if retry_trigger and task in state.get("errors", {}):
        _retry_one_task(task, now, state, model)
        return
    trigger = (
        "retry"
        if retry_trigger or task in state.get("errors", {})
        else "scheduled"
    )
    if task == "daily_summary":
        _run_daily_summaries(now.date(), state, model, target=target)
    elif task == "weekly_report":
        _run_weekly_reports(
            now.date(), state, model, trigger=trigger, target=target
        )
    elif task == "monthly_report":
        _run_monthly_reports(
            now.date(), state, model, trigger=trigger, target=target
        )


def _process_pending_targets(
    now: datetime.datetime,
    state: dict,
    model: settings.ModelDict | None,
    *,
    manual_retry: bool = False,
    process_all: bool = False,
) -> None:
    """Run queued targets in dependency order without crossing an older target."""
    maximum_rounds = 100 if process_all else 1
    for _ in range(maximum_rounds):
        progressed = False
        for task in _AUTOMATION_TASK_ORDER:
            targets = _pending_task_targets(state, task)
            if not targets:
                continue
            target = targets[0]
            if manual_retry and task in state.get("errors", {}):
                should_run = True
            else:
                should_run = _task_should_run(
                    state,
                    task,
                    now,
                    initial_detection_due=True,
                    target=target,
                )
            if not should_run:
                if task in state.get("errors", {}):
                    _save_automation_state(state)
                    return
                _dequeue_task_target(state, task, target)
                _save_automation_state(state)
                progressed = True
                if _pending_task_targets(state, task):
                    break
                continue

            if model is None:
                try:
                    model = _automation_model()
                except (KeyError, RuntimeError, TypeError) as error:
                    _set_task_error(
                        state,
                        task,
                        f"{CONFIG_ERROR_MARKER} 活动模型配置无效: {error}",
                        target=target,
                    )
                    _save_automation_state(state)
                    return
            _run_pending_task(
                task,
                target,
                now,
                state,
                model,
                retry_trigger=manual_retry,
            )
            if manual_retry:
                fresh_state = _load_automation_state()
                state.clear()
                state.update(fresh_state)
            progressed = True
            if task in state.get("errors", {}):
                return
            _dequeue_task_target(state, task, target)
            _save_automation_state(state)
            if _pending_task_targets(state, task):
                break
        if not process_all or not progressed or not _has_pending_targets(state):
            return


def run_due_automatic_tasks() -> None:
    """执行到期的日总结和闭合周期报告。"""
    automation = settings.CONFIG.get("automation", {})
    if not isinstance(automation, dict):
        logger.error("automation_configuration_invalid")
        return
    if automation.get("enabled", True) is not True:
        return
    automation_lock = _acquire_automation_lock()
    if automation_lock is None:
        return
    try:
        state = _load_automation_state()
        now = datetime.datetime.now()
        state["last_check_started_at"] = now.isoformat(timespec="seconds")
        _save_automation_state(state)
    except Exception as error:
        logger.error(
            "automation_state_initialization_failed error_type=%s",
            error.__class__.__name__,
        )
        automation_lock.release()
        return
    try:
        hourly_detection_due = state.get("last_detection_hour") != _hour_key(now)
        _scan_missing_targets(
            state,
            now,
            automation,
            hourly_detection_due=hourly_detection_due,
        )
        _save_automation_state(state)

        if _has_pending_targets(state):
            _process_pending_targets(now, state, None)
        if hourly_detection_due:
            # This is only a scheduler watermark. Writing it after the work means
            # a killed process is detected again on the next minute invocation.
            state["last_detection_hour"] = _hour_key(now)
        _clear_task_error(state, "scheduler")
        _save_automation_state(state)
    except Exception as error:
        state = _load_automation_state()
        _set_task_error(state, "scheduler", f"自动任务异常: {error}")
        _save_automation_state(state)
    finally:
        state = _load_automation_state()
        state.pop("current_task", None)
        state.pop("current_task_detail", None)
        state.pop("current_task_started_at", None)
        state["last_check_completed_at"] = datetime.datetime.now().isoformat(
            timespec="seconds"
        )
        try:
            _save_automation_state(state)
        finally:
            automation_lock.release()


def _run_weekly_reports(
    today: datetime.date,
    state: dict,
    model_config: settings.ModelDict,
    *,
    trigger: str = "scheduled",
    target: dict[str, str] | None = None,
) -> None:
    if target:
        start = datetime.date.fromisoformat(target["start"])
        end = datetime.date.fromisoformat(target["end"])
    else:
        start, end = _latest_week_period(today)
    path = _analysis_report_path("weekly", start, end, "auto")
    if not _existing_logs(start, end) or path.exists():
        _clear_task_error(state, "weekly_report")
        _save_automation_state(state)
        return
    _set_current_task(
        state,
        "weekly_report",
        f"正在生成 {start:%Y-%m-%d} 至 {end:%Y-%m-%d} 自动周报",
    )
    message, success, _ = generate_analysis_report(
        "weekly", start, model_config, origin="auto", trigger=trigger
    )
    if success:
        _clear_task_error(state, "weekly_report")
    else:
        _set_task_error(
            state,
            "weekly_report",
            f"自动生成截至 {end:%Y-%m-%d} 的周报失败: {message[:500]}",
            target=target or {"start": start.isoformat(), "end": end.isoformat()},
        )
    _save_automation_state(state)


def _run_monthly_reports(
    today: datetime.date,
    state: dict,
    model_config: settings.ModelDict,
    *,
    trigger: str = "scheduled",
    target: dict[str, str] | None = None,
) -> None:
    if target:
        start = datetime.date.fromisoformat(target["start"])
        end = datetime.date.fromisoformat(target["end"])
    else:
        start, end = _latest_month_period(today)
    path = _analysis_report_path("monthly", start, end, "auto")
    if not _existing_logs(start, end) or path.exists():
        _clear_task_error(state, "monthly_report")
        _save_automation_state(state)
        return
    _set_current_task(state, "monthly_report", f"正在生成 {start:%Y-%m} 自动月报")
    message, success, _ = generate_analysis_report(
        "monthly", start, model_config, origin="auto", trigger=trigger
    )
    if success:
        _clear_task_error(state, "monthly_report")
    else:
        _set_task_error(
            state,
            "monthly_report",
            f"自动生成 {start:%Y-%m} 月报失败: {message[:500]}",
            target=target or {"start": start.isoformat(), "end": end.isoformat()},
        )
    _save_automation_state(state)


AUTOMATION_TASK_LABELS = {
    "daily_summary": "日总结",
    "weekly_report": "自动周报",
    "monthly_report": "自动月报",
}


def failed_automatic_tasks() -> list[tuple[str, str, str]]:
    """Return retryable failures as ``(task, label, error)`` tuples."""
    errors = _load_automation_state().get("errors", {})
    return [
        (task, AUTOMATION_TASK_LABELS[task], str(errors[task]))
        for task in AUTOMATION_TASK_LABELS
        if task in errors
    ]


def _retry_one_task(
    task: str,
    now: datetime.datetime,
    state: dict,
    model: settings.ModelDict,
) -> None:
    target = _stored_task_target(state, task, now)
    if task == "daily_summary":
        _run_daily_summaries(now.date(), state, model, target=target)
    elif task == "weekly_report":
        _run_weekly_reports(
            now.date(), state, model, trigger="retry", target=target
        )
    elif task == "monthly_report":
        _run_monthly_reports(
            now.date(), state, model, trigger="retry", target=target
        )


def retry_failed_automatic_tasks() -> tuple[str, bool]:
    """Retry current failures in dependency order until one still fails."""
    automation_lock = _acquire_automation_lock()
    if automation_lock is None:
        return "另一个自动任务正在运行，请稍后重试。", False
    state: dict = {}
    tasks: list[str] = []
    try:
        state = _load_automation_state()
        _save_automation_state(state)
        tasks = [
            task
            for task in AUTOMATION_TASK_LABELS
            if task in state.get("errors", {})
        ]
        if not tasks:
            return "当前没有失败的自动任务可重试。", True
        now = datetime.datetime.now()
        for task in tasks:
            _enqueue_task_target(state, task, _stored_task_target(state, task, now))
        state["last_retry_started_at"] = now.isoformat(timespec="seconds")
        _save_automation_state(state)
        logger.info("automation_retry_started tasks=%s", ",".join(tasks))
        _process_pending_targets(
            now,
            state,
            None,
            manual_retry=True,
            process_all=True,
        )
        state = _load_automation_state()
        remaining = [
            task for task in _AUTOMATION_TASK_ORDER if task in state.get("errors", {})
        ]
        success = not remaining and not _has_pending_targets(state)
        logger.info(
            "automation_retry_completed success=%s remaining=%s",
            success,
            ",".join(remaining),
        )
        if success:
            return "全部失败自动任务重试成功。", True
        if not remaining:
            return "失败任务已恢复，但仍有排队中的后续自动任务。", False
        labels = "、".join(AUTOMATION_TASK_LABELS[task] for task in remaining)
        return f"以下自动任务仍失败：{labels}", False
    except Exception as error:
        state = _load_automation_state()
        _set_task_error(
            state,
            "scheduler",
            f"后台重试全部自动任务异常: {error}",
        )
        _save_automation_state(state)
        logger.error(
            "automation_retry_failed error_type=%s",
            error.__class__.__name__,
        )
        return str(error), False
    finally:
        try:
            if tasks:
                state = _load_automation_state()
                state.pop("current_task", None)
                state.pop("current_task_detail", None)
                state.pop("current_task_started_at", None)
                state["last_retry_completed_at"] = datetime.datetime.now().isoformat(
                    timespec="seconds"
                )
                _save_automation_state(state)
        finally:
            automation_lock.release()


def automation_status_snapshot() -> dict:
    """汇总真实产物状态、调度时间和当前失败。"""
    state = _load_automation_state()
    now = datetime.datetime.now()
    return {
        "last_check_started_at": state.get("last_check_started_at", ""),
        "last_check_completed_at": state.get("last_check_completed_at", ""),
        "last_retry_started_at": state.get("last_retry_started_at", ""),
        "last_retry_completed_at": state.get("last_retry_completed_at", ""),
        "current_task": state.get("current_task", ""),
        "current_task_detail": state.get("current_task_detail", ""),
        "current_task_started_at": state.get("current_task_started_at", ""),
        "daily_summary_status": _task_artifact_status("daily_summary", now),
        "weekly_report_status": _task_artifact_status("weekly_report", now),
        "monthly_report_status": _task_artifact_status("monthly_report", now),
        "last_detection_hour": state.get("last_detection_hour", ""),
        "retry_after": dict(state.get("retry_after", {})),
        "retry_kind": dict(state.get("retry_kind", {})),
        "failure_counts": dict(state.get("failure_counts", {})),
        "pending_targets": {
            task: _pending_task_targets(state, task)
            for task in _AUTOMATION_TASK_ORDER
            if _pending_task_targets(state, task)
        },
        "errors": dict(state.get("errors", {})),
    }
