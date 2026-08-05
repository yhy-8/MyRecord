"""Interactive record/report mode application loop."""

import datetime

from rich.panel import Panel

from .. import journal, settings
from .commands import (
    _handle_analysis,
    _handle_reference,
    _handle_retry,
    _handle_status,
    _handle_summary,
    _handle_view,
)
from .report_jobs import manual_report_jobs
from .terminal import (
    RECORD_MODE,
    REPORT_MODE,
    console,
    safe_input,
    show_help,
)


MODE_COMMANDS = {
    RECORD_MODE: ("/h", "/mode", "/v", "/ref", "/d", "/c"),
    REPORT_MODE: ("/h", "/mode", "/status", "/s", "/a", "/retry", "/m"),
}


def _command_token(user_input: str) -> str:
    return user_input.split(maxsplit=1)[0]


def run_interactive() -> None:
    current_model = settings.ModelConfig.get_model()
    mode = RECORD_MODE
    console.print(Panel.fit("[bold]AgentRecord 思维记录系统[/bold]", border_style="cyan"))
    show_help(mode)
    console.print()

    while True:
        try:
            raw_input = safe_input(">> ")
            submitted_at = datetime.datetime.now()
            user_input = raw_input.strip()
        except (KeyboardInterrupt, EOFError):
            running = manual_report_jobs.running_label()
            if running:
                console.print(
                    f"[yellow][!][/yellow] {running}仍在后台生成，"
                    "当前退出已取消；完成前请不要关闭窗口。"
                )
                continue
            console.print("[dim]系统退出。[/dim]")
            break
        if not user_input:
            continue

        command = _command_token(user_input)
        if command == "/mode":
            mode = REPORT_MODE if mode == RECORD_MODE else RECORD_MODE
            show_help(mode)
            continue
        if command == "/h":
            show_help(mode)
            continue
        allowed_commands = MODE_COMMANDS[mode]
        known_commands = set(MODE_COMMANDS[RECORD_MODE] + MODE_COMMANDS[REPORT_MODE])
        if command in known_commands and command not in allowed_commands:
            target = "报告" if mode == RECORD_MODE else "记录"
            console.print(f"[yellow][!][/yellow] 请先用 /mode 切换到{target}模式。")
            continue

        if mode == REPORT_MODE:
            if command == "/status":
                _handle_status()
            elif command == "/m":
                next_model = settings.ModelConfig.next_after(current_model["name"])
                try:
                    current_model = settings.ModelConfig.select(next_model["name"])
                except OSError as error:
                    console.print(f"[red][!][/red] 模型切换失败: {error}")
                    continue
                console.print(
                    f"[cyan][*][/cyan] 模型已永久切换为: {current_model['name']}"
                )
            elif command == "/s" and (
                user_input == "/s" or user_input.startswith("/s ")
            ):
                _handle_summary(user_input, current_model)
            elif command == "/a" and (
                user_input == "/a" or user_input.startswith("/a ")
            ):
                if _handle_analysis(user_input, current_model):
                    mode = RECORD_MODE
                    console.print("[dim]已返回记录模式。[/dim]")
            elif command == "/retry":
                if _handle_retry():
                    mode = RECORD_MODE
                    console.print("[dim]已返回记录模式。[/dim]")
            else:
                console.print("[yellow][!][/yellow] 报告模式只接受当前帮助中的命令。")
            continue

        if user_input == "/c":
            console.clear()
        elif user_input == "/d":
            if journal.delete_last_record():
                console.print("[cyan][*][/cyan] 已删除今日最后一条记录。")
            else:
                console.print("[yellow][!][/yellow] 今日无记录可删除。")
        elif user_input == "/v" or user_input.startswith("/v "):
            _handle_view(user_input)
        elif user_input == "/ref" or user_input.startswith("/ref "):
            _handle_reference(user_input)
        else:
            journal.append_log(user_input, submitted_at=submitted_at)
