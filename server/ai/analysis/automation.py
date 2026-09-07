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
import re
from pathlib import Path

from ...hub.atomic_write import atomic_write

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
# 「正在生成」状态超过该秒数仍未收尾 → 视为上次运行中断（崩溃/重启），允许重新调度。
_RUNNING_STALE_SECONDS = 600

# 时间基准固定 UTC+8（非配置项），与 store.today_utc8() 同源：自动任务的目标周期
# （昨/上周/上月）必须以 UTC+8 自然日为准，否则在非 UTC+8 时区的服务器上与日界
# 封存所依据的“今天”错位，会对错误的日期范围生成总结/报告。
_UTC8 = datetime.timezone(datetime.timedelta(hours=8))


def _now() -> datetime.datetime:
    """当前 UTC+8 墙钟时间（naive，不携带时区信息）。

    返回 naive 是为了与既有状态文件（无时区后缀）及测试保持兼容；系统整体固定
    UTC+8，无需额外携带时区标记。
    """
    return datetime.datetime.now(tz=_UTC8).replace(tzinfo=None)


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


def _target_key(target: dict[str, str]) -> str:
    """任务状态里持久化的周期标识（start|end）。"""
    return f"{target['start']}|{target['end']}"


def _same_period(record: dict, target: dict[str, str]) -> bool:
    """判断某任务记录是否仍然对应当前目标周期。

    跨周期（昨 / 上周 / 上月滚动）后，旧周期记录被丢弃，只保留当前周期——重试状态
    只在一个周期内有效（前天的日记失败了，到了今天就不管前天，只处理昨天）。
    target_key 缺失（旧状态文件 / 刚创建）视为当前，向后兼容。
    """
    return record.get("target_key") in (None, _target_key(target))


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


def _purge_empty_placeholder_days() -> None:
    """删除数据目录下所有「空占位」日记文件（正文无任何记录标记 / **HH:MM** 头行）。

    无记录日不再生成文件（见 §3.2），但这会清除历史部署遗留的空占位文件，使
    ``_task_state`` 以「文件存在性」即可判定有无内容。仅删除无任何记录的文件
    （不含用户数据）：某日曾有记录又被全部删除时，正文含 tombstone 标记，不会被删除。
    """
    directory = settings.DIARY_DIR
    if not directory.is_dir():
        return
    for path in directory.glob("*.md"):
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        body = re.sub(r"<summary>.*?</summary>", "", content, flags=re.DOTALL)
        is_placeholder = not (
            re.search(r"myrecord-(?:time|device|tombstone-time):", body)
            or re.search(r"\*\*\d{2}:\d{2}", body)
        )
        if is_placeholder:
            try:
                path.unlink()
                logger.info("purged_empty_placeholder date=%s", path.stem)
            except OSError:
                pass


def _diary_summary_needs_generation(path: Path) -> bool:
    try:
        summary = journal.extract_summary(path.read_text(encoding="utf-8")).strip()
    except OSError:
        return False
    return summary in {"", "(无)", "(无总结)", "暂无今日总结。"}


def _task_state(task: str, now: datetime.datetime, *, target=None) -> str:
    """按「产物文件是否存在 + 目标周期」判定任务当前状态（严格以文件为准）。

    返回三种状态：
    - ``empty``：该周期**没有任何日记内容**（昨日无日记文件 / 周月周期内无日志），无事可做。
    - ``done``：**产物已存在**（昨日总结已写非占位正文 / 周月报告文件已生成）。
    - ``missing``：**有内容但无产物**，需要（重新）生成。

    判定规则（与设计基线 §9.1 / §9.2 一致）：
    - 每日总结：昨日日记文件不存在（无记录日不生成文件，见 §3.2）→ ``empty``；
      文件存在则看 ``<summary>`` 是否为默认占位（``missing``）或已有正文（``done``）。
    - 周/月报：该周期内无任何日记文件（无实质记录）→ ``empty``；有日志但报告文件
      不存在 → ``missing``；报告文件已生成 → ``done``。
    """
    target = target or _default_task_target(task, now)
    if task == "daily_summary":
        day = datetime.date.fromisoformat(target["start"])
        path = settings.DIARY_DIR / f"{day.isoformat()}.md"
        if not path.exists():
            return "empty"
        return "missing" if _diary_summary_needs_generation(path) else "done"
    kind = "weekly" if task == "weekly_report" else "monthly"
    start = datetime.date.fromisoformat(target["start"])
    end = datetime.date.fromisoformat(target["end"])
    if not _existing_logs(start, end):
        return "empty"
    path = _analysis_report_path(kind, start, end)
    return "missing" if not path.exists() else "done"


def _task_missing(task: str, now: datetime.datetime, *, target=None) -> bool:
    """是否缺失（= 有内容但无产物）。"""
    return _task_state(task, now, target=target) == "missing"


# ---------- 生成与重试 ----------


def _retry_limit(task: str) -> int:
    return settings.retry_policy()[_RETRY_LIMIT_KEYS[task]]


def _model_ready() -> tuple[bool, str]:
    """返回 (是否能生成, 不可用原因)。

    配置错误 / 活动模型缺 api_key 时不可生成。这类问题靠重试无法自愈（改配置需重启），
    故不计入「失败（待重试）」，而记作独立的「未配置（无AI）」状态。
    """
    try:
        model = settings.ModelConfig.get_model()
    except Exception as error:
        return False, f"模型配置无效: {error}"
    api_url = str(model.get("api_url") or "").strip()
    api_key = str(model.get("api_key") or "").strip()
    if not api_url:
        return False, "活动模型 api_url 为空"
    if not api_key:
        return False, "活动模型 api_key 为空"
    return True, ""


def _is_stale_running(record: dict, now: datetime.datetime) -> bool:
    """「正在生成」是否已超时未收尾（上次运行中断/崩溃），允许重新调度。"""
    if record.get("status") != "running":
        return False
    started = record.get("started_at", "")
    if not started:
        return True
    try:
        return now >= datetime.datetime.fromisoformat(started) + datetime.timedelta(
            seconds=_RUNNING_STALE_SECONDS
        )
    except ValueError:
        return True


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


def _mark_ok(record: dict, tkey: str | None = None) -> None:
    record.update(
        status="ok", error="", attempts=0, started_at="", next_retry_at=""
    )
    if tkey is not None:
        record["target_key"] = tkey


def _mark_empty(record: dict, tkey: str | None = None) -> None:
    """该周期无任何记录：无事可做，记为「无内容」，区别于「已生成」。"""
    record.update(
        status="empty", error="", attempts=0, started_at="", next_retry_at=""
    )
    if tkey is not None:
        record["target_key"] = tkey


def _mark_unconfigured(record: dict, message: str, tkey: str | None = None) -> None:
    """模型未配置/不可用：记录原因，不进入可重试的失败状态。"""
    record.update(
        status="unconfigured",
        error=message,
        attempts=0,
        started_at="",
        next_retry_at="",
    )
    if tkey is not None:
        record["target_key"] = tkey


def _mark_failure(
    record: dict,
    task: str,
    message: str,
    now: datetime.datetime,
    tkey: str | None = None,
) -> None:
    attempts = int(record.get("attempts", 0) or 0) + 1
    limit = _retry_limit(task)
    # limit 是「首次之后再重试的次数」，共执行 limit+1 次；超过上限即停止
    if attempts > limit:
        record.update(
            status="blocked",
            error=message,
            attempts=attempts,
            started_at="",
            next_retry_at="",
        )
    else:
        record.update(
            status="failed",
            error=message,
            attempts=attempts,
            started_at="",
            next_retry_at=_now_text(
                now + datetime.timedelta(minutes=_RETRY_INTERVAL_MINUTES)
            ),
        )
    if tkey is not None:
        record["target_key"] = tkey
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
    """每 15 分钟：严格按文件判定任务状态（empty/done/missing），刷新对应状态。

    以文件为准：产物文件已生成 → 已生成（ok）；周期无记录 → 无内容（empty）；
    有内容但无产物 → 新缺失则置为「待生成（到期）」。已进入失败/重试安排的（failed/blocked）
    保留其重试计划，交由 _process_due 按 next_retry_at 处理。
    """
    for task in _AUTOMATION_TASKS:
        if automation.get(task, True) is not True:
            state.get("tasks", {}).pop(task, None)
            continue
        target = _default_task_target(task, now)
        record = _task_record(state, task)
        tkey = _target_key(target)
        # 周期已滚动：丢弃上个周期的失败/重试状态，只保留当前周期（昨/上周/上月）
        if not _same_period(record, target):
            record.clear()
        st = _task_state(task, now, target=target)
        if st == "done":
            _mark_ok(record, tkey)
            continue
        if st == "empty":
            _mark_empty(record, tkey)
            continue
        # missing：有内容但无产物。仅当尚未进入失败/重试安排时置为立即到期；
        # 同周期已失败的保留其重试安排（_process_due 会按 next_retry_at 处理）。
        if not record or record.get("status") in {"ok", "empty"}:
            record.update(
                status="pending",
                error="",
                attempts=0,
                started_at="",
                next_retry_at=_now_text(now),
                target_key=tkey,
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
        target = _default_task_target(task, now)
        record = _task_record(state, task)
        tkey = _target_key(target)
        # 两次检测之间恰好跨周期：丢弃旧周期失败状态，只保留当前周期
        if not _same_period(record, target):
            record.clear()
            record.update(
                status="pending",
                error="",
                attempts=0,
                started_at="",
                next_retry_at=_now_text(now),
                target_key=tkey,
            )
        # 崩溃/中断恢复：上次「正在生成」长期未收尾 → 视为可重试
        if _is_stale_running(record, now):
            record.update(
                status="pending",
                error="",
                attempts=0,
                started_at="",
                next_retry_at=_now_text(now),
                target_key=tkey,
            )
        # 先按文件现状刷新产物状态（可能在此期间已生成/空置）
        st = _task_state(task, now, target=target)
        if st == "done":
            _mark_ok(record, tkey)
            continue
        if st == "empty":
            _mark_empty(record, tkey)
            continue
        # 到这里：有内容待生成。
        # 已完成（ok）/已达上限（blocked）无需再生成；正在生成且未超时则等待。
        if record.get("status") in {"ok", "blocked"}:
            continue
        if record.get("status") == "running" and not _is_stale_running(record, now):
            continue
        # 未配置（无AI）：记录原因；配置修好后（_model_ready 通过）自然进入生成。
        ready, reason = _model_ready()
        if not ready:
            _mark_unconfigured(record, reason, tkey)
            continue
        # 配置就绪：unconfigured 立即到期；pending 立即到期；失败按 next_retry_at。
        if record.get("status") != "unconfigured" and not _retry_due(record, now):
            continue
        # 标记「正在生成」并持久化，使 /status 在生成期间可见（生成可能耗时较长）。
        record.update(
            status="running",
            error="",
            started_at=_now_text(now),
            next_retry_at="",
        )
        _save_automation_state(state)
        logger.info("automation_task_start task=%s", task)
        message, success = _run_generation(task, target)
        if success:
            _mark_ok(record, tkey)
            logger.info("automation_task_completed task=%s", task)
        else:
            _mark_failure(record, task, message, now, tkey)


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
        now = _now()
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
        state["last_check_completed_at"] = _now_text(_now())
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
        now = _now()
        failed = [
            task
            for task in _AUTOMATION_TASKS
            if automation.get(task, True) is True
            and _task_record(state, task).get("status") in _FAILED_STATUSES
            and _same_period(_task_record(state, task), _default_task_target(task, now))
        ]
        if not failed:
            return True, "当前没有失败的自动任务可重试。"
        for task in failed:
            _task_record(state, task).update(
                status="pending",
                error="",
                attempts=0,
                next_retry_at=_now_text(now),
                target_key=_target_key(_default_task_target(task, now)),
            )
        _process_due(state, now, automation)
        _save_automation_state(state)
        remaining = [
            task
            for task in _AUTOMATION_TASKS
            if automation.get(task, True) is True
            and _task_record(state, task).get("status") in _FAILED_STATUSES
            and _same_period(_task_record(state, task), _default_task_target(task, now))
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
        state["last_retry_completed_at"] = _now_text(_now())
        try:
            _save_automation_state(state)
        finally:
            lock.release()


# ---------- 状态查询 ----------


def failed_automatic_tasks() -> list[tuple[str, str, str]]:
    """返回当前周期尚未完成的失败任务 ``(task, label, error)`` 列表。

    跨周期后旧周期失败任务将被丢弃（见 _same_period），因此这里只列当前周期的失败。
    """
    state = _load_automation_state()
    now = _now()
    return [
        (task, AUTOMATION_TASK_LABELS[task], str(_task_record(state, task).get("error", "")))
        for task in _AUTOMATION_TASKS
        if _task_record(state, task).get("status") in _FAILED_STATUSES
        and _same_period(_task_record(state, task), _default_task_target(task, now))
    ]


def automation_status_snapshot() -> dict:
    """汇总任务状态与调度时间（供状态查看）。"""
    state = _load_automation_state()
    now = _now()
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