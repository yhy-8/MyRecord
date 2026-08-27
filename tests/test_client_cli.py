"""客户端 CLI 调度测试。

覆盖新薄客户端的统一单模式交互（共 8 个命令）：
- 每个命令的调度路由
- 普通输入 → 生成 entry 并写本地 + 即时 push
- 启动时一次性 full_sync（连上云端对账）
- /sync 手动完整同步
- /d 在线删除当天最新一条（服务端确认，入垃圾桶）
- /model 按服务端模型列表循环切换
"""

import unittest
from unittest.mock import Mock, patch

from client.cli import app as cli_app
from client.cli import view as cli_view
from client import credentials as client_credentials
from client import terminal as client_terminal


class ClientCLIHelpTests(unittest.TestCase):
    def test_help_catalogues_all_eight_commands(self):
        text = cli_app._help_text()
        for command in ("/v", "/c", "/h", "/sync", "/d", "/status", "/retry", "/model"):
            self.assertIn(command, text)
        # 不再区分模式（旧 /mode 已移除）
        self.assertNotIn(" 模式", text)


class ClientCLIPlainInputTests(unittest.TestCase):
    def test_plain_input_builds_entry_and_writes_and_pushes(self):
        with patch.object(cli_app, "journal") as journal, patch.object(
            cli_app, "idseq"
        ) as idseq, patch.object(client_credentials, "require") as require:
            idseq.make_entry_id.return_value = "e2e-a-1"
            journal.append_record = Mock()
            require.return_value = {"device_id": "e2e-a", "token": "t"}

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
            client_credentials, "require"
        ) as require, patch.object(cli_app, "journal") as journal, patch.object(
            cli_app, "idseq"
        ) as idseq, patch.object(client_terminal, "clear_screen") as clear, patch.object(
            cli_view, "show_day"
        ) as show_day:
            require.return_value = {"device_id": "e2e-a", "token": "t"}
            idseq.make_entry_id.return_value = "e2e-a-1"
            journal.append_record = Mock()
            client = Mock()
            client.base_url = "http://127.0.0.1:1"
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
            client_credentials, "require"
        ) as require, patch.object(cli_app, "journal") as journal, patch.object(
            cli_app, "idseq"
        ) as idseq:
            require.return_value = {"device_id": "e2e-a", "token": "t"}
            idseq.make_entry_id.return_value = "e2e-a-1"
            journal.append_record = Mock()
            SyncClient.return_value = Mock()
            SyncClient.return_value.base_url = "http://127.0.0.1:1"

            with patch("builtins.input", side_effect=[EOFError]):
                cli_app.run_interactive()

            journal.append_record.assert_not_called()


if __name__ == "__main__":
    unittest.main()