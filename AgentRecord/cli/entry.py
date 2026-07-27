"""Process entry point shared by scripts, module execution, and PyInstaller."""

import ctypes
import logging
import sys


logger = logging.getLogger(__name__)


def _hide_background_console() -> None:
    """Hide only the Windows packaged background-task console."""
    if sys.platform != "win32" or not {
        "--run-automation",
        "--retry-automation",
    }.intersection(sys.argv):
        return
    try:
        window = ctypes.windll.kernel32.GetConsoleWindow()
        if window:
            ctypes.windll.user32.ShowWindow(window, 0)
    except (AttributeError, OSError):
        pass


_hide_background_console()


def _handle_process_action(arguments: list[str]) -> bool:
    from ..analysis import (
        install_system_automation,
        retry_failed_automatic_tasks,
        run_due_automatic_tasks,
        uninstall_system_automation,
    )

    if "--run-automation" in arguments:
        run_due_automatic_tasks()
        return True
    if "--retry-automation" in arguments:
        retry_failed_automatic_tasks()
        return True
    from .terminal import console

    if "--install-automation" in arguments:
        success, message = install_system_automation()
        console.print(f"[{'green' if success else 'red'}]{message}[/]")
        if not success:
            raise SystemExit(1)
        return True
    if "--uninstall-automation" in arguments:
        success, message = uninstall_system_automation()
        console.print(f"[{'green' if success else 'red'}]{message}[/]")
        if not success:
            raise SystemExit(1)
        return True
    return False


def _show_automation_status() -> None:
    from ..analysis import automation_status_snapshot
    from .terminal import console

    status = automation_status_snapshot()
    color = "green" if status["installed"] else "yellow"
    marker = "*" if status["installed"] else "!"
    console.print(f"[{color}][{marker}] {status['install_message']}[/{color}]")
    if status["errors"]:
        retry_kind = status.get("retry_kind", {})
        failure_labels = {
            "network": "网络错误",
            "rate_limit": "接口限流",
            "blocked": "配置/鉴权错误",
            "content_blocked": "内容/格式失败，已暂停自动重试",
        }
        tasks = "、".join(
            f"{task}（{failure_labels.get(retry_kind.get(task), '内容或格式错误')}）"
            for task in status["errors"]
        )
        console.print(
            f"[yellow][!] 自动任务存在未恢复失败：{tasks}；"
            "请切换到报告模式用 /status 查看实际重试时间，或执行 /retry 立即按依赖顺序重试。[/yellow]"
        )


def _show_configuration_warnings() -> None:
    from .. import settings
    from .terminal import console

    for warning in settings.configuration_warnings():
        console.print(f"[yellow][!] {warning}[/yellow]")


def main() -> None:
    from ..logging_config import configure_logging
    from .. import settings

    configure_logging()
    action = next(
        (argument for argument in sys.argv[1:] if argument.startswith("--")),
        "interactive",
    )
    logger.info("application_started action=%s", action)
    for warning in settings.configuration_warnings():
        logger.warning("configuration_warning detail=%s", warning)
    if _handle_process_action(sys.argv[1:]):
        return
    _show_configuration_warnings()
    _show_automation_status()
    from .app import run_interactive

    run_interactive()
