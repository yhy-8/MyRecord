"""客户端本地助手测试：凭据读写/require、单调序号持久化、客户端入口。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from client import config as client_config
from client import identity
from client import __main__ as client_main


class ClientIdentityCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "credentials.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_returns_empty_when_missing(self):
        with patch.object(identity, "credentials_path", return_value=self.path):
            self.assertEqual({}, identity.load())

    def test_save_then_load_roundtrip(self):
        with patch.object(identity, "credentials_path", return_value=self.path):
            identity.save("token-123", "device-A")
            value = identity.load()

        self.assertEqual({"device_id": "device-A", "token": "token-123"}, value)

    def test_load_rejects_malformed_or_incomplete(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        with patch.object(identity, "credentials_path", return_value=self.path):
            self.assertEqual({}, identity.load())

        self.path.write_text('{"device_id": "d"}', encoding="utf-8")
        with patch.object(identity, "credentials_path", return_value=self.path):
            self.assertEqual({}, identity.load())

    def test_require_fails_when_unconfigured(self):
        with patch.object(identity, "credentials_path", return_value=self.path):
            with self.assertRaisesRegex(RuntimeError, "未配置凭据"):
                identity.require()

    def test_require_returns_value_when_configured(self):
        with patch.object(identity, "credentials_path", return_value=self.path):
            identity.save("t", "d")
            self.assertEqual({"device_id": "d", "token": "t"}, identity.require())


class ClientEntryIdTests(unittest.TestCase):
    """设备无关的确定性 entry_id：同一记录在任何客户端都得到同一个 id。"""

    def test_entry_id_deterministic_and_device_independent(self):
        i1 = identity.make_entry_id("2024-06-01", 1717200000, "第一条")
        i2 = identity.make_entry_id("2024-06-01", 1717200000, "第一条")
        self.assertEqual(i1, i2)  # 同记录同 id（跨客户端一致）
        self.assertTrue(i1.startswith("e"))

        # 不同时间写相同文字 → 不同 id（不误合并同一天重复句子）
        self.assertNotEqual(i1, identity.make_entry_id("2024-06-01", 1717200060, "第一条"))
        # 不同文字 → 不同 id
        self.assertNotEqual(i1, identity.make_entry_id("2024-06-01", 1717200000, "另一条"))
        # 不同日期 → 不同 id
        self.assertNotEqual(i1, identity.make_entry_id("2024-06-02", 1717200000, "第一条"))

    def test_device_name_defaults_to_hostname(self):
        with patch.object(identity, "load", return_value={}):
            self.assertTrue(identity.device_name())

    def test_device_name_prefers_override(self):
        with patch.object(identity, "load", return_value={"token": "t", "device_id": "vivo y78"}):
            self.assertEqual("vivo y78", identity.device_name())


class ClientConfigTests(unittest.TestCase):
    def test_default_server_url_is_https(self):
        """默认服务端地址必须为 https（自签证书直连，TLS 强制）。"""
        missing = Path(tempfile.mkdtemp(prefix="cfg-")) / "config.yaml"
        with patch.object(client_config, "config_path", return_value=missing):
            value = client_config.load()
        self.assertEqual("https://localhost:8765", value["client"]["server_url"])


class ClientMainTests(unittest.TestCase):
    def test_main_runs_interactive_and_returns_zero(self):
        with patch.object(client_main, "run_interactive") as run:
            rc = client_main.main()

        self.assertEqual(0, rc)
        run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()