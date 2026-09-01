"""客户端 CLI 调度测试。

覆盖新薄客户端的统一单模式交互（共 8 个命令）：
- 每个命令的调度路由
- 普通输入 → 生成 entry 并写本地 + 即时 push
- 启动时 full_sync（连上云端对账）
- /sync 手动完整同步
- /d 在线删除当天最新一条（服务端确认，入垃圾桶）
- /model 按服务端模型列表循环切换
"""

import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from client import config as client_config
from client import identity as client_identity
from client import journal as client_journal
from client import sync as client_sync
from client.sync import SyncClient

from client import cli as cli_app


class ClientCLIHelpTests(unittest.TestCase):
    def test_help_catalogues_all_eight_commands(self):
        text = cli_app._help_text()
        for command in ("/v", "/c", "/h", "/sync", "/d", "/status", "/retry", "/model"):
            self.assertIn(command, text)
        # 不再区分模式（旧 /mode 已移除）
        self.assertNotIn(" 模式", text)


class ClientCLIUnconfiguredOfflineTests(unittest.TestCase):
    """本地优先：无凭据/离线时仍能本地记录，push 静默失败并留离线队列。"""

    def test_plain_input_records_locally_without_credentials(self):
        root = Path(tempfile.mkdtemp(prefix="cli-nocred-"))
        records = root / "Records"
        records.mkdir(parents=True, exist_ok=True)
        entry = {
            "entry_id": "e-test1",
            "device_id": "desk-01",
            "date": "2024-06-01",
            "ts": 5,
            "tag": "",
            "text": "无凭据本地记录",
        }
        # 无凭据 + 死端口：真实 SyncClient 与真实 journal
        with patch.object(client_sync, "_state_path", return_value=root / "state.json"), patch.object(
            client_sync, "_outbox_path", return_value=root / "outbox.json"
        ), patch.object(
            client_config,
            "load",
            return_value={
                "client": {
                    "server_url": "http://127.0.0.1:1",
                    "records_dir": records,
                    "analysis_dir": root / "AnalysisReports",
                    "log_dir": root / "Log",
                    "longpoll_timeout_seconds": 25,
                    "verify": "",
                }
            },
        ), patch.object(client_identity, "load", return_value={}):
            client = SyncClient()
            client.push_new(entry)  # 无凭据：内部静默失败，不抛异常
            client_journal.append_record(entry)  # 本地记录永不依赖凭据/在线

            day = (records / "2024-06-01.md").read_text(encoding="utf-8")
            self.assertIn("无凭据本地记录", day)
            self.assertIn("e-test1", day)

        # 离线队列保留，等待拿到凭据后上线冲刷
        outbox = json.loads((root / "outbox.json").read_text(encoding="utf-8"))
        self.assertEqual(1, len(outbox["entries"]))

    def test_plain_input_records_locally_without_records_dir(self):
        """回归：全新客户端（Records/ 尚不存在）也能立即本地记录并进离线队列。

        修复前 file_lock 在锁文件父目录缺失时抛 FileNotFoundError，
        客户端首次记录即崩溃；这里不预创建 Records/，验证本地记录永不依赖目录存在。
        """
        root = Path(tempfile.mkdtemp(prefix="cli-fresh-"))
        records = root / "Records"  # 故意不预创建：模拟全新安装首次记录
        entry = {
            "entry_id": "e-fresh-1",
            "device_id": "desk-01",
            "date": "2026-09-01",
            "ts": 5,
            "tag": "",
            "text": "首次本地记录",
        }
        with patch.object(client_sync, "_state_path", return_value=root / "state.json"), patch.object(
            client_sync, "_outbox_path", return_value=root / "outbox.json"
        ), patch.object(
            client_config,
            "load",
            return_value={
                "client": {
                    "server_url": "http://127.0.0.1:1",
                    "records_dir": records,
                    "analysis_dir": root / "AnalysisReports",
                    "log_dir": root / "Log",
                    "longpoll_timeout_seconds": 25,
                    "verify": "",
                }
            },
        ), patch.object(client_identity, "load", return_value={}):
            client = SyncClient()
            # 与 _write_record 一致：先本地落盘（首次不崩溃），再进离线队列
            client_journal.append_record(entry)
            client.push_new(entry)  # 无凭据：内部静默失败，不抛异常

            day = (records / "2026-09-01.md").read_text(encoding="utf-8")
            self.assertIn("首次本地记录", day)
            self.assertIn("e-fresh-1", day)

        # 离线队列保留，等待拿到凭据后上线冲刷
        outbox = json.loads((root / "outbox.json").read_text(encoding="utf-8"))
        self.assertEqual(1, len(outbox["entries"]))


class ClientCLIPlainInputTests(unittest.TestCase):
    def test_plain_input_builds_entry_and_writes_and_pushes(self):
        with patch.object(cli_app, "journal") as journal, patch.object(
            client_identity, "make_entry_id"
        ) as make_entry_id, patch.object(client_identity, "device_name") as device_name:
            make_entry_id.return_value = "e2e-a-1"
            device_name.return_value = "e2e-a"
            journal.append_record = Mock()

            client = Mock()
            cli_app._write_record(client, "普通记录")

            entry = client.push_new.call_args.args[0]
            self.assertEqual("e2e-a-1", entry["entry_id"])
            self.assertEqual("e2e-a", entry["device_id"])
            self.assertEqual("普通记录", entry["text"])
            self.assertIn("ts", entry)
            journal.append_record.assert_called_once_with(entry)


class ClientCLICommandRoutingTests(unittest.TestCase):
    def test_dispatch_routes_known_commands(self):
        with patch.object(cli_app, "SyncClient") as SyncClient, patch.object(
            client_identity, "require"
        ) as require, patch.object(cli_app, "journal") as journal, patch.object(
            client_identity, "make_entry_id"
        ) as make_entry_id, patch.object(cli_app, "clear_screen") as clear, patch.object(
            cli_app, "show_day"
        ) as show_day:
            require.return_value = {"device_id": "e2e-a", "token": "t"}
            make_entry_id.return_value = "e2e-a-1"
            journal.append_record = Mock()
            client = Mock()
            client.base_url = "http://127.0.0.1:1"
            client.probe.return_value = {"connected": True, "has_credentials": True, "error": ""}
            client.status.return_value = {
                "version": 3,
                "entry_count": 2,
                "tombstone_count": 1,
                "devices": {"e2e-a": {"active": True}},
                "ai": {
                    "current_model": "deepseek-v4-pro",
                    "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
                },
                "automation": {},
            }
            SyncClient.return_value = client

            with patch(
                "builtins.input",
                side_effect=["/c", "/h", "/v", "/sync", "/d", "/status", "/model", EOFError],
            ):
                cli_app.run_interactive()

            clear.assert_called_once()
            show_day.assert_called_once()
            # 启动时 full_sync 一次，/sync 手动再触发一次
            client.full_sync.assert_called()
            client.delete_latest.assert_called_once()  # 当天最新一条
            client.status.assert_called()
            # /model 切到服务端模型列表的下一个
            client.admin_set_model.assert_called_once_with("deepseek-v4-flash")
            # 命令都不应写入日记
            journal.append_record.assert_not_called()

    def test_eof_exit_writes_no_record(self):
        with patch.object(cli_app, "SyncClient") as SyncClient, patch.object(
            client_identity, "require"
        ) as require, patch.object(cli_app, "journal") as journal, patch.object(
            client_identity, "make_entry_id"
        ) as make_entry_id:
            require.return_value = {"device_id": "e2e-a", "token": "t"}
            make_entry_id.return_value = "e2e-a-1"
            journal.append_record = Mock()
            SyncClient.return_value = Mock()
            SyncClient.return_value.base_url = "http://127.0.0.1:1"
            SyncClient.return_value.probe.return_value = {"connected": True, "has_credentials": True, "error": ""}

            with patch("builtins.input", side_effect=[EOFError]):
                cli_app.run_interactive()

            journal.append_record.assert_not_called()


class ClientCLISlashDispatchTests(unittest.TestCase):
    """统一命令解析：仅开头的 `/` 视为命令；未知命令提示而不写入日记。"""

    def _run_with_inputs(self, inputs):
        with patch.object(cli_app, "SyncClient") as SyncClient, patch.object(
            client_identity, "require"
        ) as require, patch.object(cli_app, "journal") as journal, patch.object(
            client_identity, "make_entry_id"
        ) as make_entry_id, patch.object(client_identity, "device_name") as device_name, patch(
            "builtins.input", side_effect=inputs
        ):
            require.return_value = {"device_id": "e2e-a", "token": "t"}
            make_entry_id.return_value = "e2e-a-1"
            device_name.return_value = "e2e-a"
            journal.append_record = Mock()
            SyncClient.return_value = Mock()
            SyncClient.return_value.base_url = "http://127.0.0.1:1"
            SyncClient.return_value.probe.return_value = {"connected": True, "has_credentials": True, "error": ""}
            cli_app.run_interactive()
            return journal

    def test_unknown_slash_command_rejected_not_written(self):
        """`/statuss` 是命令拼写错误：应提示未知命令，而不是写进日记。"""
        journal = self._run_with_inputs(["/statuss", EOFError])
        journal.append_record.assert_not_called()

    def test_non_leading_slash_is_written_as_record(self):
        """`/` 只当首字符时才是命令；出现在正文中间按普通文本记录。"""
        journal = self._run_with_inputs(["我在公司 / 写代码", EOFError])
        journal.append_record.assert_called_once()


class ClientCLIRenderDayTests(unittest.TestCase):
    def test_render_day_markdown_converts_summary_to_quote(self):
        content = """# 2026-09-01

<summary>
暂无今日总结。
</summary>

---
## 原始记录流

**19:02 [MK8]:** /statuss
"""
        rendered = cli_app._render_day_markdown(content)
        self.assertIn("> 暂无今日总结。", rendered)
        # 自定义 <summary> 标签本身应被移除
        self.assertNotIn("<summary>", rendered)
        # 其余 Markdown 原样保留
        self.assertIn("## 原始记录流", rendered)
        self.assertIn("**19:02 [MK8]:** /statuss", rendered)

    def test_render_day_markdown_keeps_summary_markdown(self):
        content = """# 2026-09-01

<summary>
第一条\n\n第二条
</summary>
"""
        rendered = cli_app._render_day_markdown(content)
        self.assertIn("> 第一条", rendered)
        self.assertIn("> 第二条", rendered)


class ClientCLIDateTests(unittest.TestCase):
    """/v 与终端日期解析辅助（已并入 cli/app.py）。"""

    def test_resolve_date_defaults_to_today(self):
        self.assertEqual(
            datetime.date.today().isoformat(), cli_app.resolve_date("")
        )
        self.assertEqual(
            datetime.date.today().isoformat(), cli_app.resolve_date("today")
        )
        self.assertEqual(
            datetime.date.today().isoformat(), cli_app.resolve_date("今天")
        )

    def test_resolve_date_handles_negative_and_explicit_dates(self):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        self.assertEqual(yesterday, cli_app.resolve_date("yesterday"))
        self.assertEqual(yesterday, cli_app.resolve_date("昨天"))
        self.assertEqual(yesterday, cli_app.resolve_date("-1"))
        self.assertEqual("2024-01-02", cli_app.resolve_date("2024-01-02"))

    def test_resolve_date_rejects_unparseable(self):
        self.assertEqual("", cli_app.resolve_date("xyz"))

    def test_show_day_prints_missing_message_for_absent_date(self):
        with patch.object(cli_app, "journal") as journal:
            journal.day_path.return_value = Path("/nonexistent.md")
            with patch("builtins.print") as mock_print:
                cli_app.show_day("2024-01-02")
        self.assertIn("还没有记录", mock_print.call_args.args[0])


if __name__ == "__main__":
    unittest.main()