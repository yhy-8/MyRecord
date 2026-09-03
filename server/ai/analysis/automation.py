"""自动任务调度（简单调度器，无失败类型区分）。

三种任务（日总结 / 周报 / 月报）互不依赖、独立调度：

- **检测**：每 15 分钟扫描一次。通过「产物文件是否存在 + 当前时间确定目标周期」判断
  任务是否缺失（昨天总结未写、上一完整自然周/月报未生成）。新发现缺失 → 立即到期执行。
- **生成**：产物缺失且到期时生成一次（报告只读本周期原始记录流，互不依赖，顺序无关）。
- **失败重试**：不区分任何失败类型。失败后**立刻开始计时**，30 分钟后自动重试；每尝试一次
  累加 `attempts`，达到该任务的重试上限（默认 2，即最多执行 3 次）后停止自动重试。
- **手动 /retry**：直接重试**全部**失败任务（重置计数立即再试一轮），不做顺序约束。

每个任务的状态（完成 / 失败+原因 / 下次重试时间 / 已达上限）持久化在
`AnalysisReports/.automation-state.json`。
"""

import datetime
import json
import logging
from pathlib import Path

from common.atomic_write import atomic_write

from .. import journal, settings
from ..file_lock import FileLock
from .context import (
    _analysis_report_path,
    _existing_logs,
)
from .orchestrator import (
    generate_analysis_report,
    summarize_diary,
)


logger = logging.getLogger(__name__)

_DETECTION_INTERVAL_MINUTES = 15
_RETRY_INTERVAL_MINUTES = 30

_AUTOMATION_TASKS = ("daily_summary", "weekly_report", "monthly_report")
_RETRY_LIMIT_KEYS = {
    "daily_summary": "daily_summary_retry_limit",
    "weekly_report": "weekly_report_retry_limit",
    "monthly_report": "monthly_report_retry_limit",
}
AUTOMATION_TASK_LABELS = {
    "daily_summary": "日总结",
    "weekly_report": "自动周报",
    "monthly_report": "自动月报",
}
# status: "ok"（完成）| "pending"（到期待生成）| "failed"（失败，等待重试）| "blocked"（已达上限）
_FAILED_STATUSES = {"failed", "blocked"}


# ---------- 状态文件 ----------


def _load_automation_state() -> dict:
    path = settings.ANALYSIS_DIR / ".automation-state.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def _save_automation_state(state: dict) -> None:
    path = settings.ANALYSIS_DIR / ".automation-state.json"
    atomic_write(path, json.dumps(state, ensure_ascii=False, indent=2))


def _task_record(state: dict, task: str) -> dict:
    return state.setdefault("tasks", {}).setdefault(task, {})


def _automation_lock() -> FileLock | None:
    return FileLock.acquire(settings.ANALYSIS_DIR / ".automation.lock")


def _now_text(now: datetime.datetime) -> str:
    return now.isoformat(timespec="seconds")


# ---------- 目标周期 / 缺失判定 ----------


def _latest_week_period(today: datetime.date) -> tuple[datetime.date, datetime.date]:
    end = today - datetime.timedelta(days=today.weekday() + 1)
    return end - datetime.timedelta(days=6), end


def _latest_month_period(today: datetime.date) -> tuple[datetime.date, datetime.date]:
    end = today.replace(day=1) - datetime.timedelta(days=1)
    return end.replace(day=1), end


def _default_task_target(task: str, now: datetime.datetime) -> dict[str, str]:
    today = now.date()
    if task == "daily_summary":
        day = today - datetime.timedelta(days=1)
        return {"start": day.isoformat(), "end": day.isoformat()}
    if task == "weekly_report":
        start, end = _latest_week_period(today)
        return {"start": start.isoformat(), "end": end.isoformat()}
    start, end = _latest_month_period(today)
    return {"start": start.isoformat(), "end": end.isoformat()}


def _diary_summary_needs_generation(path: Path) -> bool:
    try:
        summary = journal.extract_summary(path.read_text(encoding="utf-8")).strip()
    except OSError:
        return False
    return summary in {"", "(无)", "暂无今日总结。"}


def _task_missing(task: str, now: datetime.datetime, *, target=None) -> bool:
    """按「产物文件是否存在 + 目标周期」判定任务是否缺失。"""
    target = target or _default_task_target(task, now)
    if task == "daily_summary":
        day = datetime.date.fromisoformat(target["start"])
        path = settings.DIARY_DIR / f"{day.isoformat()}.md"
        return path.exists() and _diary_summary_needs_generation(path)
    kind = "weekly" if task == "weekly_report" else "monthly"
    start = datetime.date.fromisoformat(target["start"])
    end = datetime.date.fromisoformat(target["end"])
    path = _analysis_report_path(kind, start, end)
    return bool(_existing_logs(start, end)) and not path.exists()


# ---------- 生成与重试 ----------


def _retry_limit(task: str) -> int:
    return settings.retry_policy()[_RETRY_LIMIT_KEYS[task]]


def _run_generation(task: str, target: dict[str, str]) -> tuple[str, bool]:
    model = settings.ModelConfig.get_model()
    if task == "daily_summary":
        return summarize_diary(target["start"], model)
    kind = "weekly" if task == "weekly_report" else "monthly"
    message, success, _ = generate_analysis_report(
        kind,
        datetime.date.fromisoformat(target["start"]),
        model,
    )
    return message, success


def _retry_due(record: dict, now: datetime.datetime) -> bool:
    if record.get("status") == "pending":
        return True
    when = record.get("next_retry_at", "")
    if not when:
        return False
    try:
        return now >= datetime.datetime.fromisoformat(when)
    except ValueError:
        return False


def _mark_ok(record: dict) -> None:
    record.update(status="ok", error="", attempts=0, next_retry_at="")


def _mark_failure(
    record: dict, task: str, message: str, now: datetime.datetime
) -> None:
    attempts = int(record.get("attempts", 0) or 0) + 1
    limit = _retry_limit(task)
    # limit 是「首次之后再重试的次数」，共执行 limit+1 次；超过上限即停止
    if attempts > limit:
        record.update(
            status="blocked", error=message, attempts=attempts, next_retry_at=""
        )
    else:
        record.update(
            status="failed",
            error=message,
            attempts=attempts,
            next_retry_at=_now_text(
                now + datetime.timedelta(minutes=_RETRY_INTERVAL_MINUTES)
            ),
        )
    logger.warning(
        "automation_task_failed task=%s attempts=%s limit=%s",
        task,
        attempts,
        limit,
    )


# ---------- 检测 / 执行 ----------


def _detection_due(state: dict, now: datetime.datetime) -> bool:
    last = state.get("last_detection_at", "")
    if not last:
        return True
    try:
        return now >= datetime.datetime.fromisoformat(last) + datetime.timedelta(
            minutes=_DETECTION_INTERVAL_MINUTES
        )
    except ValueError:
        return True


def _scan_missing(
    state: dict,
    now: datetime.datetime,
    automation: dict,
) -> None:
    """每 15 分钟：为新缺失任务初始化「到期」，产物已存在则标记完成。"""
    for task in _AUTOMATION_TASKS:
        if automation.get(task, True) is not True:
            state.get("tasks", {}).pop(task, None)
            continue
        target = _default_task_target(task, now)
        record = _task_record(state, task)
        if not _task_missing(task, now, target=target):
            _mark_ok(record)
            continue
        # 新缺失（无记录或曾完成又缺失）→ 立即到期；已失败的保留自身重试安排
        if not record or record.get("status") == "ok":
            record.update(
                status="pending", error="", attempts=0, next_retry_at=_now_text(now)
            )


def _process_due(
    state: dict,
    now: datetime.datetime,
    automation: dict,
) -> None:
    """执行所有到期的缺失任务（任务间互不依赖，顺序无关）。"""
    for task in _AUTOMATION_TASKS:
        if automation.get(task, True) is not True:
            continue
        record = _task_record(state, task)
        if record.get("status") in {"ok", "blocked"} or not _retry_due(record, now):
            continue
        target = _default_task_target(task, now)
        if not _task_missing(task, now, target=target):
            _mark_ok(record)
            continue
        logger.info("automation_task_start task=%s", task)
        message, success = _run_generation(task, target)
        if success:
            _mark_ok(record)
            logger.info("automation_task_completed task=%s", task)
        else:
            _mark_failure(record, task, message, now)


def run_due_automatic_tasks() -> None:
    """每分钟入口：每 15 分钟检测一次缺失，并执行所有到期任务。"""
    automation = settings.CONFIG.get("automation", {})
    if not isinstance(automation, dict):
        logger.error("automation_configuration_invalid")
        return
    if automation.get("enabled", True) is not True:
        return
    lock = _automation_lock()
    if lock is None:
        return
    try:
        state = _load_automation_state()
        now = datetime.datetime.now()
        state["last_check_started_at"] = _now_text(now)
        _save_automation_state(state)
    except Exception as error:
        logger.error(
            "automation_state_initialization_failed error_type=%s",
            error.__class__.__name__,
        )
        lock.release()
        return
    try:
        if _detection_due(state, now):
            _scan_missing(state, now, automation)
            state["last_detection_at"] = _now_text(now)
            _save_automation_state(state)
        _process_due(state, now, automation)
        _save_automation_state(state)
    except Exception as error:
        logger.error(
            "automation_cycle_failed error_type=%s",
            error.__class__.__name__,
        )
    finally:
        state = _load_automation_state()
        state["last_check_completed_at"] = _now_text(datetime.datetime.now())
        try:
            _save_automation_state(state)
        finally:
            lock.release()


def retry_failed_automatic_tasks() -> tuple[bool, str]:
    """手动 /retry：直接重试全部失败任务（重置计数立即再试一轮，顺序无关）。

    返回 ``(ok, message)``，与 HTTP 层 (server/hub/server.py) 的解包顺序一致。
    """
    automation = settings.CONFIG.get("automation", {})
    if not isinstance(automation, dict) or automation.get("enabled", True) is not True:
        return False, "自动任务已停用。"
    lock = _automation_lock()
    if lock is None:
        return False, "另一个自动任务正在运行，请稍后重试。"
    try:
        state = _load_automation_state()
        now = datetime.datetime.now()
        failed = [
            task
            for task in _AUTOMATION_TASKS
            if automation.get(task, True) is True
            and _task_record(state, task).get("status") in _FAILED_STATUSES
        ]
        if not failed:
            return True, "当前没有失败的自动任务可重试。"
        for task in failed:
            _task_record(state, task).update(
                status="pending", error="", attempts=0, next_retry_at=_now_text(now)
            )
        _process_due(state, now, automation)
        _save_automation_state(state)
        remaining = [
            task
            for task in _AUTOMATION_TASKS
            if automation.get(task, True) is True
            and _task_record(state, task).get("status") in _FAILED_STATUSES
        ]
        if not remaining:
            return True, "全部失败自动任务重试成功。"
        labels = "、".join(AUTOMATION_TASK_LABELS[task] for task in remaining)
        return False, f"以下自动任务仍失败：{labels}"
    except Exception as error:
        logger.error(
            "automation_retry_failed error_type=%s", error.__class__.__name__
        )
        return False, f"重试失败: {error}"
    finally:
        state = _load_automation_state()
        state["last_retry_completed_at"] = _now_text(datetime.datetime.now())
        try:
            _save_automation_state(state)
        finally:
            lock.release()


# ---------- 状态查询 ----------


def failed_automatic_tasks() -> list[tuple[str, str, str]]:
    """返回失败任务 ``(task, label, error)`` 列表。"""
    state = _load_automation_state()
    return [
        (task, AUTOMATION_TASK_LABELS[task], str(_task_record(state, task).get("error", "")))
        for task in _AUTOMATION_TASKS
        if _task_record(state, task).get("status") in _FAILED_STATUSES
    ]


def automation_status_snapshot() -> dict:
    """汇总任务状态与调度时间（供状态查看）。"""
    state = _load_automation_state()
    now = datetime.datetime.now()
    tasks = {}
    for task in _AUTOMATION_TASKS:
        record = dict(_task_record(state, task))
        if record.get("status") == "failed" and _retry_due(record, now):
            record["retry_due"] = True
        tasks[task] = record
    return {
        "last_check_started_at": state.get("last_check_started_at", ""),
        "last_check_completed_at": state.get("last_check_completed_at", ""),
        "last_retry_completed_at": state.get("last_retry_completed_at", ""),
        "last_detection_at": state.get("last_detection_at", ""),
        "tasks": tasks,
    }