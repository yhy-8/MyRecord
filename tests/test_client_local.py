"""客户端本地助手测试：凭据读写/require、单调序号持久化、客户端入口。"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from client import config as client_config
from client import identity
from client import sync as client_sync
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
            identity.save("token-123")
            value = identity.load()

        self.assertEqual({"token": "token-123"}, value)

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
            identity.save("t")
            self.assertEqual({"token": "t"}, identity.require())


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

    def test_device_name_ignores_override_and_uses_hostname(self):
        # 配置里不再允许自定义设备名：即使 credentials 残留 device_id，也只用本机名
        with patch.object(identity, "load", return_value={"token": "t", "device_id": "vivo y78"}), patch.object(
            identity, "_hostname", return_value="myhost"
        ):
            self.assertEqual("myhost", identity.device_name())


class ClientConfigTests(unittest.TestCase):
    def test_default_server_url_is_https(self):
        """默认服务端地址必须为 https（自签证书直连，TLS 强制）。"""
        missing = Path(tempfile.mkdtemp(prefix="cfg-")) / "config.yaml"
        with patch.object(client_config, "config_path", return_value=missing):
            value = client_config.load()
        self.assertEqual("https://localhost:8765", value["client"]["server_url"])


class ClientFrozenPathTests(unittest.TestCase):
    """打包成 exe（sys.frozen=True）时，配置/凭据/状态/离线队列路径都应指向 exe 同级目录，
    而不是临时解压目录 _MEIPASS（进程退出即删，会导致数据丢失）。"""

    @staticmethod
    def _frozen():
        return patch.object(sys, "frozen", True, create=True)

    def test_config_path_frozen_points_next_to_executable(self):
        with self._frozen():
            self.assertEqual(
                Path(sys.executable).resolve().parent / "config.yaml",
                client_config.config_path(),
            )

    def test_sync_state_and_outbox_frozen_points_to_config_dir(self):
        with self._frozen():
            expected_dir = Path(sys.executable).resolve().parent
            self.assertEqual(expected_dir / "state.json", client_sync._state_path())
            self.assertEqual(expected_dir / "outbox.json", client_sync._outbox_path())

    def test_credentials_path_frozen_points_next_to_executable(self):
        with self._frozen():
            self.assertEqual(
                Path(sys.executable).resolve().parent / "credentials.json",
                identity.credentials_path(),
            )


class ClientMainTests(unittest.TestCase):
    def test_main_runs_interactive_and_returns_zero(self):
        with patch.object(client_main, "run_interactive") as run:
            rc = client_main.main()

        self.assertEqual(0, rc)
        run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()