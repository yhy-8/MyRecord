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

    def test_token_create_lists_and_revokes(self):
        rc, text = self._capture(["token", "create", "--device", "phone a"])
        self.assertEqual(0, rc)
        self.assertIn("device_id: phone-a", text)
        self.assertIn("token: ", text)

        store = Store(self.data_dir / "state.json")
        self.assertIn("phone-a", store.device_ids())
        token = self._token_from(text)
        self.assertIsNotNone(token)
        self.assertTrue(store.verify_device("phone-a", token))

        rc, listing = self._capture(["token", "list"])
        self.assertEqual(0, rc)
        self.assertIn("phone-a", listing)

        rc, _ = self._capture(["token", "revoke", "--device", "phone-a"])
        self.assertEqual(0, rc)
        # 重新读取 state.json，确认撤销已持久化
        fresh = Store(self.data_dir / "state.json")
        self.assertFalse(fresh.verify_device("phone-a", token))
        self.assertNotIn("phone-a", fresh.device_ids())

    def test_token_rotate_changes_token(self):
        rc, first_text = self._capture(["token", "create", "--device", "device-A"])
        self.assertEqual(0, rc)
        device_id = "device-A"
        token1 = self._token_from(first_text)
        self.assertIsNotNone(token1)

        rc, second_text = self._capture(
            ["token", "rotate", "--device", device_id]
        )
        self.assertEqual(0, rc)
        token2 = self._token_from(second_text)

        store = Store(self.data_dir / "state.json")
        self.assertNotEqual(token1, token2)
        self.assertFalse(store.verify_device(device_id, token1))
        self.assertTrue(store.verify_device(device_id, token2))

    def test_token_revoke_unknown_device_returns_error(self):
        err = io.StringIO()
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", err):
            rc = server_main.main(["token", "revoke", "--device", "missing"])
        self.assertEqual(1, rc)
        self.assertIn("设备不存在", err.getvalue())

    def test_token_create_requires_device(self):
        err = io.StringIO()
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", err):
            rc = server_main.main(["token", "create"])
        self.assertEqual(2, rc)
        self.assertIn("--device", err.getvalue())


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