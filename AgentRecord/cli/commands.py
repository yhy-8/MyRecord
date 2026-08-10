"""Record and report mode command handlers."""

import datetime
import re

from rich.markdown import Markdown
from rich.panel import Panel

from .. import journal, settings
from ..analysis import (
    analysis_report_path,
    automation_status_snapshot,
    generate_analysis_report,
    launch_automation_retry,
    summarize_diary,
)
from .report_jobs import manual_report_jobs
from .terminal import console, post_notification, safe_input, show_view_help


def _handle_view(user_input: str) -> None:
    argument = user_input[3:].strip() if user_input.startswith("/v ") else ""
    if argument.lower() == "help":
        show_view_help()
        return
    date = journal.resolve_date(argument)
    if not date:
        console.print(f"[yellow][!][/yellow] 无法解析日期: {argument}")
        return
    file_path = settings.DIARY_DIR / f"{date}.md"
    if not file_path.exists():
        console.print(f"[yellow][!][/yellow] 找不到 {date} 的记录。")
        return
    content = re.sub(r"</?summary>", "", file_path.read_text(encoding="utf-8"))
    console.print(
        Panel(Markdown(content), title=f"[bold]{date}[/bold]", border_style="cyan")
    )


def _handle_summary(user_input: str, model_config: settings.ModelDict) -> None:
    argument = user_input[3:].strip() if user_input.startswith("/s ") else ""
    date = journal.resolve_date(argument)
    if not date:
        console.print(f"[yellow][!][/yellow] 无法解析日期: {argument}")
        return
    console.print(f"[cyan][*][/cyan] 正在生成 {date} 的日记总结...")
    summary, success = summarize_diary(date, model_config)
    if success:
        console.print(
            Panel(summary, title=f"[bold]{date} 日记总结[/bold]", border_style="green")
        )
    else:
        console.print(f"[red][!][/red] 总结失败: {summary}")


def _parse_analysis_arguments(user_input: str) -> tuple[str, str]:
    arguments = user_input.split()[1:]
    kind = "weekly"
    date_argument = ""
    if not arguments:
        return kind, date_argument

    first = arguments[0].lower()
    if first in ("weekly", "week", "周报"):
        kind = "weekly"
        date_argument = " ".join(arguments[1:])
    elif first in ("monthly", "month", "月报"):
        kind = "monthly"
        date_argument = " ".join(arguments[1:])
    else:
        date_argument = " ".join(arguments)
    return kind, date_argument


def _analysis_period_choices(kind: str, limit: int = 20) -> list[tuple[str, datetime.date]]:
    dates = []
    for path in settings.DIARY_DIR.glob("*.md"):
        try:
            dates.append(datetime.date.fromisoformat(path.stem))
        except ValueError:
            continue
    periods: dict[datetime.date, set[datetime.date]] = {}
    for date in dates:
        anchor = (
            date - datetime.timedelta(days=date.weekday())
            if kind == "weekly"
            else date.replace(day=1)
        )
        periods.setdefault(anchor, set()).add(date)

    today = datetime.date.today()
    choices = []
    for anchor, recorded_dates in sorted(periods.items(), reverse=True)[:limit]:
        if kind == "weekly":
            end = anchor + datetime.timedelta(days=6)
            period_text = f"{anchor:%Y-%m-%d} 至 {end:%Y-%m-%d}"
        else:
            next_month = (anchor.replace(day=28) + datetime.timedelta(days=4)).replace(
                day=1
            )
            end = next_month - datetime.timedelta(days=1)
            period_text = f"{anchor:%Y-%m}"
        closure = "进行中" if end >= today else "已闭合"
        origins = []
        for origin, label in (("manual", "手动"), ("auto", "自动")):
            report_path = analysis_report_path(kind, anchor, origin=origin)
            if report_path and report_path.exists():
                origins.append(label)
        report_status = "+".join(origins) + "已生成" if origins else "未生成"
        choices.append(
            (
                f"{period_text}  [dim]{closure} · {len(recorded_dates)} 天有记录 · {report_status}[/dim]",
                anchor,
            )
        )
    return choices


def _choose_analysis_period(kind: str) -> datetime.date | None:
    choices = _analysis_period_choices(kind)
    if not choices:
        console.print("[yellow][!][/yellow] 没有可生成报告的记录周期。")
        return None
    content = "\n".join(
        f"  [cyan]{index}[/cyan]. {label}"
        for index, (label, _) in enumerate(choices, 1)
    )
    label = "自然周" if kind == "weekly" else "自然月"
    console.print(Panel(content, title=f"[bold]选择{label}[/bold]", border_style="cyan"))
    selection = safe_input("选择编号 [空=取消] >> ").strip()
    if not selection:
        return None
    try:
        index = int(selection) - 1
        if index < 0:
            raise IndexError
        return choices[index][1]
    except (ValueError, IndexError):
        console.print(f"[yellow][!][/yellow] 无效编号: {selection}")
        return None


def _handle_analysis(user_input: str, model_config: settings.ModelDict) -> bool:
    kind, date_argument = _parse_analysis_arguments(user_input)
    if kind in ("weekly", "monthly") and not date_argument:
        anchor = _choose_analysis_period(kind)
        if anchor is None:
            return False
        date = anchor.isoformat()
    else:
        if kind == "monthly" and re.fullmatch(r"\d{4}-\d{2}", date_argument):
            date_argument += "-01"
        date = journal.resolve_date(date_argument)
    if not date:
        console.print(f"[yellow][!][/yellow] 无法解析日期: {date_argument}")
        return False
    anchor = datetime.datetime.strptime(date, "%Y-%m-%d").date()
    label = {"weekly": "周报", "monthly": "月报"}[kind]
    target_path = analysis_report_path(kind, anchor, origin="manual")
    if target_path and target_path.exists():
        confirmation = safe_input(
            f"该周期的手动{label}已存在，确认覆盖？[y/N] >> "
        ).strip().lower()
        if confirmation not in ("y", "yes", "是"):
            console.print("[dim]已取消，原报告保持不变。[/dim]")
            return False
    started = manual_report_jobs.start(
        kind,
        anchor,
        model_config,
        generate_analysis_report,
        post_notification,
    )
    if not started:
        running = manual_report_jobs.running_label() or "手动报告"
        console.print(f"[yellow][!][/yellow] {running}仍在后台生成，请完成后再试。")
        return False
    console.print(
        f"[cyan][*][/cyan] 分析{label}已转入后台；可以继续记录，"
        "完成前请不要关闭当前窗口。"
    )
    return True


def _handle_retry() -> bool:
    started, message = launch_automation_retry()
    color = "cyan" if started else "yellow"
    marker = "*" if started else "!"
    console.print(f"[{color}][{marker}][/{color}] {message}")
    return started


def _handle_status() -> None:
    status = automation_status_snapshot()
    retry = settings.retry_policy()
    network_retry_minutes = retry["automation_network_retry_minutes"]
    content_retry_interval = retry[
        "automation_content_retry_interval_minutes"
    ]
    installed = "已安装" if status["installed"] else "未完整安装"
    lines = [
        f"  系统自动任务：{installed}",
        f"  安装详情：{status['install_message']}",
        f"  最后完成检查：{status['last_check_completed_at'] or '尚未记录'}",
        f"  最后缺漏检测小时：{status['last_detection_hour'] or '尚未记录'}",
        f"  最后手动重试：{status.get('last_retry_completed_at') or '尚未记录'}",
        f"  昨日日记总结：{status['daily_summary_status']}",
        f"  上周自动周报：{status['weekly_report_status']}",
        f"  上月自动月报：{status['monthly_report_status']}",
    ]
    if status.get("current_task"):
        lines.append(
            f"  [cyan]当前任务：{status.get('current_task_detail') or status['current_task']}"
            f"（{status.get('current_task_started_at', '')}）[/cyan]"
        )
    pending_targets = status.get("pending_targets", {})
    if pending_targets:
        lines.append("  [cyan]待处理目标（严格按以下任务顺序执行）：[/cyan]")
        for task in (
            "daily_summary",
            "weekly_report",
            "monthly_report",
        ):
            targets = pending_targets.get(task, [])
            if not targets:
                continue
            labels = [
                target["start"]
                if target.get("start") == target.get("end")
                else f"{target.get('start')} 至 {target.get('end')}"
                for target in targets
            ]
            lines.append(f"    - {task}: {'、'.join(labels)}")
    errors = status["errors"]
    if errors:
        lines.append("  [yellow]当前失败：[/yellow]")
        retry_after = status.get("retry_after", {})
        retry_kind = status.get("retry_kind", {})
        failure_counts = status.get("failure_counts", {})
        for task, message in errors.items():
            deadline = retry_after.get(task, "")
            kind = retry_kind.get(task)
            if task == "scheduler":
                lines.append(
                    f"    - scheduler [调度器错误]: {message}；"
                    "下一分钟自动重新检查"
                )
                continue
            failure_type = {
                "network": "网络错误",
                "rate_limit": "接口限流",
                "blocked": "配置/鉴权错误",
                "content_blocked": "内容/格式失败，已暂停自动重试",
            }.get(kind, "非网络错误（内容或格式错误）")
            retry_policy = {
                "network": f"{network_retry_minutes} 分钟后重试",
                "rate_limit": f"{network_retry_minutes} 分钟后重试",
                "blocked": "修正配置后自动解锁，也可用 /retry 重试",
                "content_blocked": "输入或模型变化后自动解锁，也可用 /retry 重试",
            }.get(kind, f"到下一个 {content_retry_interval} 分钟边界重试")
            suffix = (
                f"；{retry_policy}（不早于 {deadline}）" if deadline else ""
            )
            if kind in {"blocked", "content_blocked"}:
                suffix = f"；{retry_policy}"
            if kind == "content_blocked":
                suffix += f"（同一输入已失败 {failure_counts.get(task, 0)} 次）"
            lines.append(
                f"    - {task} [{failure_type}]: {message}{suffix}"
            )
    else:
        lines.append("  当前失败：无")
    console.print(Panel("\n".join(lines), title="[bold]自动任务状态[/bold]", border_style="cyan"))


def _handle_reference(user_input: str) -> None:
    arguments = user_input.split()[1:]
    date_argument = arguments[0] if arguments else ""
    if len(arguments) > 1:
        console.print("[yellow][!][/yellow] /ref 只接受一个日期参数。")
        return
    if re.fullmatch(r"\d{4}-\d{2}", date_argument):
        date_filter = date_argument
    elif date_argument:
        date_filter = journal.resolve_date(date_argument)
        if not date_filter:
            console.print(f"[yellow][!][/yellow] 无法解析日期: {date_argument}")
            return
    else:
        date_filter = ""
    sources = journal.list_reference_sources(date_filter=date_filter)
    if not sources:
        suffix = f"（日期: {date_argument}）" if date_argument else ""
        console.print(f"[yellow][!][/yellow] 没有可引用的日记{suffix}。")
        return

    choices = "\n".join(
        f"  [cyan]{index}[/cyan]. {label}"
        for index, (label, _) in enumerate(sources, 1)
    )
    console.print(Panel(choices, title="[bold]选择引用来源[/bold]", border_style="cyan"))
    selection = safe_input("选择编号 [空=取消] >> ").strip()
    if not selection:
        return
    try:
        selected_index = int(selection) - 1
        if selected_index < 0:
            raise IndexError
        label, source_path = sources[selected_index]
    except (ValueError, IndexError):
        console.print(f"[yellow][!][/yellow] 无效编号: {selection}")
        return

    note = safe_input("关联记录 [可留空] >> ").strip()
    submitted_at = datetime.datetime.now()
    journal.append_reference(label, source_path, note, submitted_at=submitted_at)
    console.print(f"[cyan][*][/cyan] 已引用: {label}")
