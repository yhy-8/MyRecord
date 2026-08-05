import datetime
import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import main as root_main
from AgentRecord import settings
from AgentRecord.cli import app, commands, entry, terminal


class MainCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.original_diary_dir = settings.DIARY_DIR
        self.original_analysis_dir = settings.ANALYSIS_DIR
        settings.DIARY_DIR = root / "Records"
        settings.ANALYSIS_DIR = root / "AnalysisReports"
        settings.DIARY_DIR.mkdir()

    def tearDown(self):
        settings.DIARY_DIR = self.original_diary_dir
        settings.ANALYSIS_DIR = self.original_analysis_dir
        self.temp_dir.cleanup()

    def test_reference_command_selects_diary_and_records_note(self):
        diary = settings.DIARY_DIR / "2026-07-14.md"
        diary.write_text("日记", encoding="utf-8")
        submitted_at = datetime.datetime(2026, 7, 15, 10, 30)
        datetime_module = Mock()
        datetime_module.datetime.now.return_value = submitted_at

        with patch(
            "AgentRecord.cli.commands.safe_input",
            side_effect=["1", "由旧日记展开的想法"],
        ), patch.object(commands, "datetime", datetime_module):
            commands._handle_reference("/ref 2026-07-14")

        content = (settings.DIARY_DIR / "2026-07-15.md").read_text(encoding="utf-8")
        self.assertIn("[引用]", content)
        self.assertIn("日记 | 2026-07-14", content)
        self.assertIn("由旧日记展开的想法", content)

    def test_reference_without_date_selects_diary_without_type_prompt(self):
        diary = settings.DIARY_DIR / "2026-07-14.md"
        diary.write_text("日记", encoding="utf-8")

        with patch(
            "AgentRecord.cli.commands.safe_input", side_effect=["1", ""]
        ) as safe_input:
            commands._handle_reference("/ref")

        self.assertEqual(2, safe_input.call_count)
        self.assertIn("选择编号", safe_input.call_args_list[0].args[0])

    def test_parses_monthly_analysis_command(self):
        self.assertEqual(
            ("monthly", "2026-07-15"),
            commands._parse_analysis_arguments("/a monthly 2026-07-15"),
        )

    @patch(
        "AgentRecord.cli.commands.generate_analysis_report",
        return_value=("月报", True, Path("report.md")),
    )
    @patch("AgentRecord.cli.commands.post_notification")
    def test_monthly_analysis_accepts_year_month(self, notify, generate_report):
        started = commands._handle_analysis("/a monthly 2026-07", {"name": "mock"})
        commands.manual_report_jobs.wait(1)

        self.assertTrue(started)
        self.assertEqual("monthly", generate_report.call_args.args[0])
        self.assertEqual(datetime.date(2026, 7, 1), generate_report.call_args.args[1])
        self.assertEqual("manual", generate_report.call_args.kwargs["origin"])
        self.assertIn("分析月报已完成", notify.call_args.args[0])

    @patch("AgentRecord.cli.commands.post_notification")
    def test_only_one_manual_report_runs_per_window(self, notify):
        worker_started = threading.Event()
        release_worker = threading.Event()

        def slow_report(kind, anchor, model_config, *, origin):
            worker_started.set()
            release_worker.wait(2)
            return "完成", True, Path("report.md")

        with patch(
            "AgentRecord.cli.commands.generate_analysis_report",
            side_effect=slow_report,
        ):
            try:
                first = commands._handle_analysis(
                    "/a monthly 2026-07", {"name": "mock"}
                )
                self.assertTrue(worker_started.wait(1))
                second = commands._handle_analysis(
                    "/a weekly 2026-07-15", {"name": "mock"}
                )
            finally:
                release_worker.set()
                commands.manual_report_jobs.wait(1)

        self.assertTrue(first)
        self.assertFalse(second)
        notify.assert_called_once()

    @patch("AgentRecord.cli.commands.generate_analysis_report")
    def test_existing_manual_report_requires_confirmation(self, generate_report):
        report = (
            settings.ANALYSIS_DIR
            / "Weekly"
            / "2026-07-13_to_2026-07-19_manual.md"
        )
        report.parent.mkdir(parents=True)
        report.write_text("已有手动报告", encoding="utf-8")

        with patch("AgentRecord.cli.commands.safe_input", return_value="n"):
            commands._handle_analysis("/a weekly 2026-07-15", {"name": "mock"})

        generate_report.assert_not_called()

    @patch(
        "AgentRecord.cli.commands.generate_analysis_report",
        return_value=("周报", True, Path("report.md")),
    )
    @patch("AgentRecord.cli.commands.post_notification")
    def test_weekly_analysis_without_date_uses_period_selector(self, notify, generate):
        for date in ("2026-07-07", "2026-07-09", "2026-07-14"):
            (settings.DIARY_DIR / f"{date}.md").write_text("记录", encoding="utf-8")

        with patch("AgentRecord.cli.commands.safe_input", return_value="1"):
            started = commands._handle_analysis("/a weekly", {"name": "mock"})
        commands.manual_report_jobs.wait(1)

        self.assertTrue(started)
        self.assertEqual(datetime.date(2026, 7, 13), generate.call_args.args[1])

    def test_status_command_displays_artifacts_and_failure(self):
        snapshot = {
            "installed": True,
            "install_message": "已安装",
            "last_check_started_at": "2026-07-15T20:00:00",
            "last_check_completed_at": "2026-07-15T20:01:00",
            "last_retry_started_at": "",
            "last_retry_completed_at": "",
            "current_task": "",
            "current_task_detail": "",
            "current_task_started_at": "",
            "daily_summary_status": "2026-07-14 已存在",
            "weekly_report_status": "2026-07-06 至 2026-07-12 缺失",
            "monthly_report_status": "2026-06 已存在",
            "last_detection_hour": "2026-07-15T20",
            "retry_after": {"weekly_report": "2026-07-15T21:00:00"},
            "retry_kind": {"weekly_report": "hourly"},
            "pending_targets": {
                "weekly_report": [
                    {"start": "2026-07-06", "end": "2026-07-12"}
                ]
            },
            "errors": {"weekly_report": "周报失败"},
        }
        with patch(
            "AgentRecord.cli.commands.automation_status_snapshot",
            return_value=snapshot,
        ), patch("AgentRecord.cli.commands.console.print") as console_print:
            commands._handle_status()

        output = str(console_print.call_args.args[0].renderable)
        self.assertIn("2026-07-14", output)
        self.assertIn("待处理目标", output)
        self.assertIn("weekly_report: 2026-07-06 至 2026-07-12", output)
        self.assertIn("weekly_report", output)
        self.assertIn("非网络错误", output)
        self.assertNotIn("weekly_report [网络错误]", output)

    def test_help_commands_are_separated_by_mode(self):
        self.assertEqual(
            {"/h", "/mode", "/v", "/ref", "/d", "/c"},
            set(app.MODE_COMMANDS[terminal.RECORD_MODE]),
        )
        self.assertEqual(
            {"/h", "/mode", "/status", "/s", "/a", "/retry", "/m"},
            set(app.MODE_COMMANDS[terminal.REPORT_MODE]),
        )

    def test_status_is_not_executed_in_record_mode(self):
        with patch(
            "AgentRecord.cli.app.safe_input", side_effect=["/status", EOFError]
        ), patch("AgentRecord.cli.app.show_help"), patch(
            "AgentRecord.cli.app._handle_status"
        ) as handle_status, patch("AgentRecord.cli.app.journal.append_log") as append:
            app.run_interactive()

        handle_status.assert_not_called()
        append.assert_not_called()

    def test_report_help_expands_analysis_subcommands(self):
        with patch("AgentRecord.cli.terminal.console.print") as console_print:
            terminal.show_help(terminal.REPORT_MODE)

        content = str(console_print.call_args.args[0].renderable)
        self.assertNotIn("/a daily [日期]", content)
        self.assertIn("/a weekly [日期]", content)
        self.assertIn("/a monthly [日期]", content)
        self.assertIn("/retry", content)

    def test_retry_launches_all_failures_in_detached_process(self):
        with patch(
            "AgentRecord.cli.commands.launch_automation_retry",
            return_value=(True, "已启动"),
        ) as launch, patch("AgentRecord.cli.commands.console.print"):
            self.assertTrue(commands._handle_retry())
        launch.assert_called_once_with()

    def test_root_main_is_only_the_shared_entry(self):
        self.assertIs(root_main.main, entry.main)

    def test_interactive_startup_shows_automation_status(self):
        with patch(
            "AgentRecord.analysis.automation_status_snapshot",
            return_value={
                "installed": True,
                "install_message": "系统自动任务已安装。",
                "errors": {},
            },
        ), patch("AgentRecord.cli.terminal.console.print") as console_print:
            entry._show_automation_status()

        console_print.assert_called_once()
        self.assertIn("已安装", str(console_print.call_args.args[0]))

    def test_failed_automation_install_returns_nonzero_exit(self):
        with patch(
            "AgentRecord.analysis.install_system_automation",
            return_value=(False, "安装失败"),
        ), patch("AgentRecord.cli.terminal.console.print"):
            with self.assertRaisesRegex(SystemExit, "1"):
                entry._handle_process_action(["--install-automation"])

    def test_windows_background_entry_hides_console_window(self):
        windll = Mock()
        windll.kernel32.GetConsoleWindow.return_value = 123
        with patch.object(entry.sys, "platform", "win32"), patch.object(
            entry.sys, "argv", ["AgentRecord.exe", "--run-automation"]
        ), patch.object(entry.ctypes, "windll", windll, create=True):
            entry._hide_background_console()

        windll.user32.ShowWindow.assert_called_once_with(123, 0)

    def test_windows_terminal_reconfigures_stdio_as_utf8(self):
        stdout_bytes = io.BytesIO()
        stderr_bytes = io.BytesIO()
        stdout = io.TextIOWrapper(stdout_bytes, encoding="cp1252")
        stderr = io.TextIOWrapper(stderr_bytes, encoding="cp1252")
        with patch.object(terminal.sys, "platform", "win32"), patch.object(
            terminal.sys, "stdout", stdout
        ), patch.object(terminal.sys, "stderr", stderr):
            terminal._configure_utf8_stdio()
            stdout.write("选择日记")
            stdout.flush()

        self.assertEqual("utf-8", stdout.encoding.lower())
        self.assertEqual("utf-8", stderr.encoding.lower())
        self.assertEqual("选择日记", stdout_bytes.getvalue().decode("utf-8"))

    def test_windows_backspace_removes_one_chinese_character(self):
        output = io.BytesIO()
        stream = io.TextIOWrapper(output, encoding="utf-8")
        windows_console = Mock()
        windows_console.kbhit.return_value = True
        windows_console.getwch.side_effect = ["中", "\x08", "\r"]

        with patch.object(
            terminal, "_windows_console_input_handle", return_value=None
        ), patch.object(
            terminal, "msvcrt", windows_console, create=True
        ), patch.object(terminal.sys, "stdout", stream):
            value = terminal._safe_input_windows(">> ")
            stream.flush()

        self.assertEqual("", value)
        rendered = output.getvalue().decode("utf-8")
        self.assertIn("\r\x1b[0J>> ", rendered)
        self.assertNotIn("\x1b8", rendered)

    def test_windows_backspace_redraw_is_relative_after_input_wraps(self):
        output = io.BytesIO()
        stream = io.TextIOWrapper(output, encoding="utf-8")
        windows_console = Mock()
        windows_console.kbhit.return_value = True
        windows_console.getwch.side_effect = [
            "甲",
            "乙",
            "丙",
            "丁",
            "\x08",
            "\r",
        ]

        with patch.object(
            terminal, "_windows_console_input_handle", return_value=None
        ), patch.object(
            terminal, "msvcrt", windows_console, create=True
        ), patch.object(
            terminal.shutil,
            "get_terminal_size",
            return_value=terminal.os.terminal_size((8, 24)),
        ), patch.object(terminal.sys, "stdout", stream):
            value = terminal._safe_input_windows(">> ")
            stream.flush()

        self.assertEqual("甲乙丙", value)
        rendered = output.getvalue().decode("utf-8")
        self.assertIn("\x1b[1A\r\x1b[0J>> 甲乙丙", rendered)
        self.assertNotIn("\x1b7", rendered)
        self.assertNotIn("\x1b8", rendered)

    def test_windows_wrap_math_handles_exact_width_and_wide_characters(self):
        self.assertEqual(
            1,
            terminal._wrapped_input_rows(">> ", list("abcde"), 8),
        )
        self.assertEqual(
            1,
            terminal._wrapped_input_rows(">> ", list("中文a"), 8),
        )
        self.assertEqual(
            0,
            terminal._wrapped_input_rows(">> ", list("中文"), 8),
        )

    def test_windows_backspace_at_exact_terminal_width_clears_one_wrapped_row(self):
        output = io.BytesIO()
        stream = io.TextIOWrapper(output, encoding="utf-8")
        windows_console = Mock()
        windows_console.kbhit.return_value = True
        windows_console.getwch.side_effect = [
            "a",
            "b",
            "c",
            "d",
            "e",
            "\x08",
            "\r",
        ]

        with patch.object(
            terminal, "_windows_console_input_handle", return_value=None
        ), patch.object(
            terminal, "msvcrt", windows_console, create=True
        ), patch.object(
            terminal.shutil,
            "get_terminal_size",
            return_value=terminal.os.terminal_size((8, 24)),
        ), patch.object(terminal.sys, "stdout", stream):
            value = terminal._safe_input_windows(">> ")
            stream.flush()

        self.assertEqual("abcd", value)
        self.assertIn(
            "\x1b[1A\r\x1b[0J>> abcd",
            output.getvalue().decode("utf-8"),
        )

    def test_windows_native_reader_consumes_unicode_key_event(self):
        kernel32 = Mock()
        kernel32.WaitForSingleObject.return_value = 0

        def read_console(_handle, event_pointer, _length, count_pointer):
            event = event_pointer._obj
            event.EventType = terminal._KEY_EVENT
            event.Event.KeyEvent.KeyDown = 1
            event.Event.KeyEvent.RepeatCount = 1
            event.Event.KeyEvent.Character.UnicodeChar = ord("中")
            count_pointer._obj.value = 1
            return 1

        kernel32.ReadConsoleInputW.side_effect = read_console
        windll = Mock(kernel32=kernel32)

        with patch.object(terminal.ctypes, "windll", windll, create=True):
            result = terminal._read_windows_input_event(123, 50)

        self.assertEqual(["中"], result)
        kernel32.WaitForSingleObject.assert_called_once()
        kernel32.ReadConsoleInputW.assert_called_once()

    def test_windows_input_record_layout_matches_win32_abi(self):
        self.assertEqual(16, terminal.ctypes.sizeof(terminal._WindowsKeyEvent))
        self.assertEqual(20, terminal.ctypes.sizeof(terminal._WindowsInputEvent))

    def test_windows_ime_commit_is_echoed_in_one_batch(self):
        windows_console = Mock()
        windows_console.kbhit.side_effect = [True, True, True, True]
        windows_console.getwch.side_effect = ["中", "文", "\r"]
        stream = Mock()

        with patch.object(
            terminal, "_windows_console_input_handle", return_value=None
        ), patch.object(
            terminal, "msvcrt", windows_console, create=True
        ), patch.object(terminal.sys, "stdout", stream):
            value = terminal._safe_input_windows(">> ")

        self.assertEqual("中文", value)
        self.assertEqual(
            [">> ", "中文\r\n"],
            [call.args[0] for call in stream.write.call_args_list],
        )

    def test_windows_non_character_event_cannot_strand_enter(self):
        stream = Mock()
        with patch.object(
            terminal, "_windows_console_input_handle", return_value=123
        ), patch.object(
            terminal,
            "_read_windows_input_event",
            side_effect=[[], ["\r"]],
        ) as read_event, patch.object(terminal.sys, "stdout", stream):
            value = terminal._safe_input_windows(">> ")

        self.assertEqual("", value)
        self.assertEqual(2, read_event.call_count)
        self.assertEqual("\r\n", stream.write.call_args_list[-1].args[0])

    @patch("AgentRecord.cli.app.journal.append_log")
    @patch("AgentRecord.cli.app.show_help")
    @patch(
        "AgentRecord.cli.app.safe_input", side_effect=["@这只是普通记录", EOFError]
    )
    def test_at_prefix_is_saved_as_plain_record(
        self, safe_input, show_help, append_log
    ):
        submitted_at = datetime.datetime(2026, 7, 16, 0, 1)
        with patch("AgentRecord.cli.app.datetime.datetime") as mock_datetime:
            mock_datetime.now.return_value = submitted_at
            app.run_interactive()

        append_log.assert_called_once()
        self.assertEqual("@这只是普通记录", append_log.call_args.args[0])
        self.assertEqual(submitted_at, append_log.call_args.kwargs["submitted_at"])

    @patch("AgentRecord.cli.app.journal.append_log")
    @patch("AgentRecord.cli.app.show_help")
    @patch(
        "AgentRecord.cli.app.safe_input",
        side_effect=["/mode", "不会误记为日记", EOFError],
    )
    def test_plain_text_in_report_mode_is_not_recorded(
        self, safe_input, show_help, append_log
    ):
        app.run_interactive()

        append_log.assert_not_called()

    @patch("AgentRecord.cli.app.journal.append_log")
    @patch("AgentRecord.cli.app._handle_analysis", return_value=True)
    @patch("AgentRecord.cli.app.show_help")
    @patch(
        "AgentRecord.cli.app.safe_input",
        side_effect=["/mode", "/a monthly 2026-07", "后台期间继续记录", EOFError],
    )
    def test_started_report_returns_to_record_mode(
        self, safe_input, show_help, handle_analysis, append_log
    ):
        app.run_interactive()

        append_log.assert_called_once()
        self.assertEqual("后台期间继续记录", append_log.call_args.args[0])

    @patch("AgentRecord.cli.app.console.print")
    @patch("AgentRecord.cli.app.show_help")
    @patch(
        "AgentRecord.cli.app.safe_input",
        side_effect=[KeyboardInterrupt, EOFError],
    )
    def test_soft_exit_is_cancelled_while_manual_report_runs(
        self, safe_input, show_help, console_print
    ):
        with patch.object(
            app.manual_report_jobs,
            "running_label",
            side_effect=["分析月报", ""],
        ):
            app.run_interactive()

        messages = [
            str(call.args[0]) for call in console_print.call_args_list if call.args
        ]
        self.assertTrue(any("当前退出已取消" in message for message in messages))

    def test_terminal_notification_queue_is_thread_safe(self):
        terminal.post_notification("后台报告完成", "green")

        self.assertEqual(
            [("后台报告完成", "green")], terminal._pending_notifications()
        )

    def test_terminal_notification_restores_current_input(self):
        output = io.BytesIO()
        stream = io.TextIOWrapper(output, encoding="utf-8")
        terminal.post_notification("后台报告完成", "green")

        with patch.object(terminal.sys, "stdout", stream), patch.object(
            terminal, "console"
        ) as console:
            terminal._show_notifications(">> ", list("正在输入"))
            stream.flush()

        self.assertTrue(output.getvalue().endswith(">> 正在输入".encode("utf-8")))
        console.print.assert_called_once_with(
            "后台报告完成", style="green", markup=False
        )

    def test_wrapped_terminal_notification_restores_wide_character_input(self):
        output = io.BytesIO()
        stream = io.TextIOWrapper(output, encoding="utf-8")
        terminal.post_notification("后台报告完成", "green")

        with patch.object(terminal.sys, "stdout", stream), patch.object(
            terminal.shutil,
            "get_terminal_size",
            return_value=terminal.os.terminal_size((8, 24)),
        ), patch.object(terminal, "console"):
            terminal._show_notifications(">> ", list("正在输入"))
            stream.flush()

        rendered = output.getvalue().decode("utf-8")
        self.assertTrue(rendered.startswith("\x1b[1A\r\x1b[0J"))
        self.assertTrue(rendered.endswith(">> 正在输入"))


if __name__ == "__main__":
    unittest.main()
