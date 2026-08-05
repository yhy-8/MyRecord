"""Public analysis API used by the CLI and automation entry points."""

from .automation import (
    _run_monthly_reports,
    _run_weekly_reports,
    automation_status_snapshot,
    failed_automatic_tasks,
    install_system_automation,
    run_due_automatic_tasks,
    launch_automation_retry,
    retry_failed_automatic_tasks,
    system_automation_status,
    uninstall_system_automation,
)
from .context import analysis_report_path
from .orchestrator import generate_analysis_report, summarize_diary

__all__ = [
    "analysis_report_path",
    "automation_status_snapshot",
    "failed_automatic_tasks",
    "generate_analysis_report",
    "install_system_automation",
    "launch_automation_retry",
    "run_due_automatic_tasks",
    "retry_failed_automatic_tasks",
    "summarize_diary",
    "system_automation_status",
    "uninstall_system_automation",
]
