"""客户端 CLI 交互命令处理器的补充测试。

聚焦 7 个命令里现有套件未充分覆盖的展示/分支：
- /status 的自动化任务逐状态文案（ok/empty/pending/running/failed/blocked/unconfigured + 原因/重试时间）
- /d 在线删除的成功 / 无记录 / 服务端异常
- /retry 重试成功 / 失败 / 服务端异常
- /model 按服务端模型列表循环下一个 / 无模型 / 服务端异常
- _write_record 对过期（非今天）输入的丢弃
- 启动状态播报（连接 vs 凭据两条独立维度）
"""

import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from client import cli as cli_app
from client.sync import SyncError

_TODAY = cli_app.today_utc8()


class FakeConsole:
    """把 rich Console.print 收集到列表，避免依赖真实终端渲染。"""

    printed = []

    def __init__(self, *a, **k):
        pass

    def print(self, *a, **k):
        text = " ".join(str(x) for x in a if x is not None)
        FakeConsole.printed.append(text)


class PrintAutomationStatusTests(unittest.TestCase):
    def setUp(self):
        FakeConsole.printed = []
        self._patch = patch.object(cli_app, "Console", FakeConsole)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_print_automation_status_labels_all_states(self):
        automation = {"tasks": {
            "daily_summary": {"status": "ok"},
            "weekly_report": {"status": "pending"},
            "monthly_report": {"status": "empty"},
        }}
        cli_app._print_automation_status(automation)
        out = "\n".join(FakeConsole.printed)
        self.assertIn("日总结: 已完成", out)
        self.assertIn("周报: 待生成", out)
        self.assertIn("月报: 无内容（该周期无记录）", out)

    def test_print_automation_status_plays_running_unconfigured(self):
        automation = {"tasks": {
            "daily_summary": {"status": "running"},
            "weekly_report": {"status": "unconfigured", "error": "活动模型 api_key 为空"},
            "monthly_report": {"status": "blocked", "error": "已达上限"},
        }}
        cli_app._print_automation_status(automation)
        out = "\n".join(FakeConsole.printed)
        self.assertIn("日总结: 正在生成", out)
        self.assertIn("周报: 未配置（无AI）", out)
        self.assertIn("活动模型 api_key 为空", out)
        self.assertIn("月报: 失败（已停止自动重试，需手动重试）", out)

    def test_print_automation_status_failed_shows_error_and_retry_at(self):
        automation = {"tasks": {
            "daily_summary": {"status": "failed", "error": "模型超时",
                              "next_retry_at": "2026-07-15T10:30:00"},
        }}
        cli_app._print_automation_status(automation)
        out = "\n".join(FakeConsole.printed)
        self.assertIn("模型超时", out)
        self.assertIn("下次重试: 2026-07-15T10:30:00", out)

    def test_print_automation_status_ignores_empty_and_non_dict(self):
        cli_app._print_automation_status({})  # 空 → 直接无输出
        cli_app._print_automation_status({"tasks": {"x": "not-a-dict"}})
        # 只打印表头，不打印任何任务行（非 dict 任务被跳过）
        self.assertEqual(["自动任务:"], FakeConsole.printed)


class HandleStatusTests(unittest.TestCase):
    def setUp(self):
        FakeConsole.printed = []
        self._patch = patch.object(cli_app, "Console", FakeConsole)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_handle_status_prints_counts_ai_automation(self):
        client = Mock()
        client.status.return_value = {
            "entry_count": 3,
            "tombstone_count": 1,
            "ai": {"current_model": "deepseek-v4-flash",
                   "models": ["deepseek-v4-flash", "deepseek-v4-pro"]},
            "automation": {"tasks": {"daily_summary": {"status": "ok"}}},
        }
        cli_app._handle_status(client)
        out = "\n".join(FakeConsole.printed)
        self.assertIn("今日条目: 3   今日已删: 1", out)
        self.assertIn("AI 模型: deepseek-v4-flash", out)
        self.assertIn("日总结: 已完成", out)

    def test_handle_status_sync_error_prints_error(self):
        client = Mock()
        client.status.side_effect = SyncError("无法连接服务端")
        cli_app._handle_status(client)
        out = "\n".join(FakeConsole.printed)
        self.assertIn("无法连接服务端", out)


class HandleDeleteTests(unittest.TestCase):
    def setUp(self):
        FakeConsole.printed = []
        self._patch = patch.object(cli_app, "Console", FakeConsole)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_handle_delete_success(self):
        client = Mock()
        client.delete_latest.return_value = "e-1"
        cli_app._handle_delete(client)
        client.delete_latest.assert_called_once_with(_TODAY)
        self.assertIn("已删除当天最新一条", "\n".join(FakeConsole.printed))

    def test_handle_delete_no_record(self):
        client = Mock()
        client.delete_latest.return_value = None
        cli_app._handle_delete(client)
        self.assertIn("当天暂无记录", "\n".join(FakeConsole.printed))

    def test_handle_delete_sync_error(self):
        client = Mock()
        client.delete_latest.side_effect = SyncError("服务端返回 500")
        cli_app._handle_delete(client)
        self.assertIn("删除失败", "\n".join(FakeConsole.printed))


class HandleRetryTests(unittest.TestCase):
    def setUp(self):
        FakeConsole.printed = []
        self._patch = patch.object(cli_app, "Console", FakeConsole)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_handle_retry_ok(self):
        client = Mock()
        client.admin_retry.return_value = {"ok": True, "message": "全部重试成功"}
        cli_app._handle_retry(client)
        self.assertIn("全部重试成功", "\n".join(FakeConsole.printed))

    def test_handle_retry_failed(self):
        client = Mock()
        client.admin_retry.return_value = {"ok": False, "message": "仍有未完成"}
        cli_app._handle_retry(client)
        self.assertIn("仍有未完成", "\n".join(FakeConsole.printed))

    def test_handle_retry_sync_error(self):
        client = Mock()
        client.admin_retry.side_effect = SyncError("鉴权失败")
        cli_app._handle_retry(client)
        self.assertIn("重试失败", "\n".join(FakeConsole.printed))


class HandleModelTests(unittest.TestCase):
    def setUp(self):
        FakeConsole.printed = []
        self._patch = patch.object(cli_app, "Console", FakeConsole)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_handle_model_cycles_to_next(self):
        client = Mock()
        client.status.return_value = {
            "entry_count": 0, "tombstone_count": 0,
            "ai": {"current_model": "a", "models": ["a", "b"]},
            "automation": {},
        }
        client.admin_set_model.return_value = {"ok": True, "message": "已永久切换为 b"}
        cli_app._handle_model(client)
        client.admin_set_model.assert_called_once_with("b")
        self.assertIn("已永久切换为 b", "\n".join(FakeConsole.printed))

    def test_handle_model_no_models(self):
        client = Mock()
        client.status.return_value = {
            "entry_count": 0, "tombstone_count": 0,
            "ai": {"current_model": "", "models": []},
            "automation": {},
        }
        cli_app._handle_model(client)
        client.admin_set_model.assert_not_called()
        self.assertIn("未配置模型", "\n".join(FakeConsole.printed))

    def test_handle_model_wraps_around(self):
        client = Mock()
        client.status.return_value = {
            "entry_count": 0, "tombstone_count": 0,
            "ai": {"current_model": "b", "models": ["a", "b"]},
            "automation": {},
        }
        client.admin_set_model.return_value = {"ok": True, "message": "已永久切换为 a"}
        cli_app._handle_model(client)
        client.admin_set_model.assert_called_once_with("a")

    def test_handle_model_sync_error(self):
        client = Mock()
        client.status.side_effect = SyncError("无法读取服务端")
        cli_app._handle_model(client)
        client.admin_set_model.assert_not_called()
        self.assertIn("无法读取服务端", "\n".join(FakeConsole.printed))


class WriteRecordExpiredTests(unittest.TestCase):
    def test_write_record_drops_non_today(self):
        with patch.object(cli_app, "journal") as journal, \
             patch.object(cli_app, "identity") as identity, \
             patch.object(cli_app, "datetime") as dt:
            # 构造一个锚定到今天，但 entry 日期算出来是“昨天”的情形
            today = datetime.date.fromisoformat(_TODAY)
            dt.datetime.now.return_value.timestamp.return_value = 0
            dt.datetime.now.timestamp.return_value = 0
            identity.make_entry_id.return_value = "e-1"
            identity.device_name.return_value = "host"
            # 用固定 ts=0 → date 由 UTC+8 推导为 1970-01-01（远早于今天）
            client = Mock()
            cli_app._write_record(client, "过期记录")
            # 过期：不 append 本地、不 push
            journal.append_record.assert_not_called()
            client.push_new.assert_not_called()


class ReportStartupStatusTests(unittest.TestCase):
    def setUp(self):
        FakeConsole.printed = []
        self._patch = patch.object(cli_app, "Console", FakeConsole)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_connected_with_credentials(self):
        client = Mock()
        client.base_url = "https://x:8765"
        cli_app._report_startup_status(client, {
            "connected": True, "has_credentials": True, "error": "",
        })
        out = "\n".join(FakeConsole.printed)
        self.assertIn("已连接服务端", out)
        self.assertIn("凭据：已配置", out)

    def test_unreachable_no_credentials(self):
        client = Mock()
        client.base_url = "https://x:8765"
        cli_app._report_startup_status(client, {
            "connected": False, "has_credentials": False,
            "error": "连接被拒绝或网络不可达",
        })
        out = "\n".join(FakeConsole.printed)
        self.assertIn("无法连接服务端", out)
        self.assertIn("连接被拒绝或网络不可达", out)
        self.assertIn("仅本地记录", out)


class ResolveDateMMDDTests(unittest.TestCase):
    def test_resolve_date_parses_mm_dd_in_current_year(self):
        self.assertEqual(f"{datetime.date.today().year}-12-25",
                         cli_app.resolve_date("12-25"))

    def test_resolve_date_parses_compact_format(self):
        result = cli_app.resolve_date("20261225")
        self.assertTrue(result.endswith("12-25"))


if __name__ == "__main__":
    unittest.main()
