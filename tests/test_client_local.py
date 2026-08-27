"""客户端本地助手测试：凭据读写/require、单调序号持久化、客户端入口。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from client import credentials, idseq, main as client_main


class ClientCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "credentials.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_returns_empty_when_missing(self):
        with patch.object(credentials, "credentials_path", return_value=self.path):
            self.assertEqual({}, credentials.load())

    def test_save_then_load_roundtrip(self):
        with patch.object(credentials, "credentials_path", return_value=self.path):
            credentials.save("device-A", "token-123")
            value = credentials.load()

        self.assertEqual({"device_id": "device-A", "token": "token-123"}, value)

    def test_load_rejects_malformed_or_incomplete(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        with patch.object(credentials, "credentials_path", return_value=self.path):
            self.assertEqual({}, credentials.load())

        self.path.write_text('{"device_id": "d"}', encoding="utf-8")
        with patch.object(credentials, "credentials_path", return_value=self.path):
            self.assertEqual({}, credentials.load())

    def test_require_fails_when_unconfigured(self):
        with patch.object(credentials, "credentials_path", return_value=self.path):
            with self.assertRaisesRegex(RuntimeError, "未配置凭据"):
                credentials.require()

    def test_require_returns_value_when_configured(self):
        with patch.object(credentials, "credentials_path", return_value=self.path):
            credentials.save("d", "t")
            self.assertEqual({"device_id": "d", "token": "t"}, credentials.require())


class ClientIdSeqTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "seq.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_next_seq_is_monotonic_and_persists(self):
        with patch.object(idseq, "seq_path", return_value=self.path):
            self.assertEqual(1, idseq.next_seq())
            self.assertEqual(2, idseq.next_seq())
            self.assertEqual(3, idseq.next_seq())

        # 重新读取（持久化跨重启）
        with patch.object(idseq, "seq_path", return_value=self.path):
            self.assertEqual(4, idseq.next_seq())

    def test_make_entry_id_combines_device_and_seq(self):
        with patch.object(idseq, "seq_path", return_value=self.path):
            self.assertEqual("device-A-1", idseq.make_entry_id("device-A"))
            self.assertEqual("device-A-2", idseq.make_entry_id("device-A"))


class ClientMainTests(unittest.TestCase):
    def test_main_runs_interactive_and_returns_zero(self):
        with patch.object(client_main, "run_interactive") as run:
            rc = client_main.main()

        self.assertEqual(0, rc)
        run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()