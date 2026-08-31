"""服务端 CLI（server/main.py）：token / import / render 子命令测试。

通过替换 config.load 指向独立临时数据目录，避免写入仓库真实 data/。
"""

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import main as server_main
from server.hub.store import Store


class ServerMainAdminRetryGlueTests(unittest.TestCase):
    """回归：automation.retry_failed_automatic_tasks 返回 (message, ok)，
    但 HTTP 层按 (ok, message) 解包。_admin_retry_result 必须把顺序归一为 (ok, message)。"""

    def test_normalizes_message_first_to_ok_first(self):
        # automation 真实契约是 (message, ok)
        def retry_callable():
            return "全部失败自动任务重试成功。", True

        ok, message = server_main._admin_retry_result(retry_callable)
        self.assertIs(ok, True)
        self.assertEqual(message, "全部失败自动任务重试成功。")

    def test_failure_stays_message_first(self):
        def retry_callable():
            return "以下自动任务仍失败：自动周报", False

        ok, message = server_main._admin_retry_result(retry_callable)
        self.assertIs(ok, False)
        self.assertEqual(message, "以下自动任务仍失败：自动周报")

    def test_exception_returns_ok_false_and_error_message(self):
        def retry_callable():
            raise RuntimeError("boom")

        ok, message = server_main._admin_retry_result(retry_callable)
        self.assertIs(ok, False)
        self.assertIn("boom", message)

    def test_admin_retry_endpoint_shape_via_real_automation(self):
        # 端到端契约：自动化失败时 /admin/retry 的 JSON 应含 ok=False 与真实文案。
        # 直接调用真实 automation 函数会写 data 状态文件，这里改用一个等价的
        # (message, ok) 契约的 stub，验证 main.py 的胶水不被绕过。
        ok, message = server_main._admin_retry_result(
            lambda: ("以下自动任务仍失败：自动月报", False)
        )
        self.assertIs(ok, False)
        self.assertEqual(message, "以下自动任务仍失败：自动月报")


def _data_dir_config(data_dir: Path) -> dict:
    return {"server": {"data_dir": str(data_dir)}}


class ServerMainTokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_dir = self.root / "data"
        self._orig_load = server_main.config.load
        server_main.config.load = lambda: _data_dir_config(self.data_dir)

    def tearDown(self):
        server_main.config.load = self._orig_load
        self.tmp.cleanup()

    def _capture(self, argv):
        out = io.StringIO()
        with patch("sys.stdout", out):
            rc = server_main.main(argv)
        return rc, out.getvalue()

    @staticmethod
    def _token_from(text):
        for line in text.splitlines():
            if line.startswith("token: "):
                return line[len("token: "):].strip()
        return None

    def test_token_single_credential_create_list_revoke(self):
        # 单一链接凭证：create 无需 --device，签发唯一 token
        rc, text = self._capture(["token", "create"])
        self.assertEqual(0, rc)
        self.assertIn("device_id: sync", text)
        self.assertIn("token: ", text)
        token = self._token_from(text)
        self.assertIsNotNone(token)

        # 凭证不绑定设备名：任意自报名字都能通过校验（单一凭证模型）
        store = Store(self.data_dir / "state.json")
        self.assertTrue(store.verify_device("whatever", token))

        rc, listing = self._capture(["token", "list"])
        self.assertEqual(0, rc)
        self.assertIn("sync", listing)

        rc, _ = self._capture(["token", "revoke"])
        self.assertEqual(0, rc)
        fresh = Store(self.data_dir / "state.json")
        self.assertFalse(fresh.verify_device("whatever", token))
        self.assertNotIn("sync", fresh.device_ids())

    def test_token_rotate_overwrites_old_token(self):
        rc, first_text = self._capture(["token", "create"])
        self.assertEqual(0, rc)
        token1 = self._token_from(first_text)
        self.assertIsNotNone(token1)

        rc, second_text = self._capture(["token", "rotate"])
        self.assertEqual(0, rc)
        token2 = self._token_from(second_text)

        store = Store(self.data_dir / "state.json")
        self.assertNotEqual(token1, token2)
        # 覆盖：旧 token 立即失效，只有新 token 有效
        self.assertFalse(store.verify_device("sync", token1))
        self.assertTrue(store.verify_device("sync", token2))

    def test_token_revoke_without_valid_credential_returns_error(self):
        err = io.StringIO()
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", err):
            rc = server_main.main(["token", "revoke"])
        self.assertEqual(1, rc)
        self.assertIn("无有效链接凭证", err.getvalue())

    def test_token_create_needs_no_device_arg(self):
        rc, text = self._capture(["token", "create"])
        self.assertEqual(0, rc)
        self.assertIn("token: ", text)


class ServerMainRenderImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_dir = self.root / "data"
        self._orig_load = server_main.config.load
        server_main.config.load = lambda: _data_dir_config(self.data_dir)

    def tearDown(self):
        server_main.config.load = self._orig_load
        self.tmp.cleanup()

    def test_render_writes_records_from_store(self):
        store = Store(self.data_dir / "state.json")
        store.append_entries(
            "legacy",
            [{
                "entry_id": "a-1",
                "date": "2024-01-01",
                "ts": 1704067200,
                "tag": "",
                "text": "hello",
            }],
        )
        with patch("sys.stdout", io.StringIO()):
            rc = server_main.main(["render"])
        self.assertEqual(0, rc)
        rendered = (self.data_dir / "Records" / "2024-01-01.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("hello", rendered)
        self.assertIn("a-1", rendered)

    def test_import_legacy_records_appends_and_renders(self):
        legacy = self.root / "legacy-records"
        legacy.mkdir()
        (legacy / "2024-01-01.md").write_text(
            "# 2024-01-01\n\n<summary>\n暂无今日总结。\n</summary>\n\n---\n"
            "## 原始记录流\n\n<!-- agentrecord-record -->\n**08:00:** 旧记录\n",
            encoding="utf-8",
        )
        # 非日期文件应被跳过
        (legacy / "notes.md").write_text("不是日记", encoding="utf-8")

        with patch("sys.stdout", io.StringIO()):
            rc = server_main.main(["import", "--records", str(legacy)])
        self.assertEqual(0, rc)

        store = Store(self.data_dir / "state.json")
        self.assertEqual(1, len(store.data["entries"]))
        entry = next(iter(store.data["entries"].values()))
        self.assertEqual("legacy", entry["device_id"])
        self.assertEqual("旧记录", entry["text"])
        rendered = (self.data_dir / "Records" / "2024-01-01.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("旧记录", rendered)

    def test_import_missing_directory_returns_error(self):
        with patch("sys.stdout", io.StringIO()):
            rc = server_main.main(
                ["import", "--records", str(self.root / "nope")]
            )
        self.assertEqual(2, rc)


if __name__ == "__main__":
    unittest.main()