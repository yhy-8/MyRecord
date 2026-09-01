"""服务端配置读取（server/config.py）：默认值、server 段合并、路径解析。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import config


class ServerConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_returns_defaults_when_no_config_file(self):
        missing = self.root / "no-config.yaml"
        with patch.object(config, "config_path", return_value=missing):
            value = config.load()

        server = value["server"]
        self.assertEqual("0.0.0.0", server["host"])
        self.assertEqual(8765, server["port"])
        # data_dir 缺省 './data'，应解析为 config 目录下的绝对路径
        self.assertEqual((missing.parent / "data").resolve(), server["data_dir"])
        self.assertEqual({}, value["raw"])

    def test_load_merges_server_section_from_yaml(self):
        config_file = self.root / "config.yaml"
        config_file.write_text(
            "server:\n"
            "  host: 127.0.0.1\n"
            "  port: 9000\n"
            "  data_dir: /abs/data\n",
            encoding="utf-8",
        )
        with patch.object(config, "config_path", return_value=config_file):
            value = config.load()

        server = value["server"]
        self.assertEqual("127.0.0.1", server["host"])
        self.assertEqual(9000, server["port"])
        self.assertEqual(Path("/abs/data"), server["data_dir"])
        self.assertEqual({"server": {"host": "127.0.0.1", "port": 9000, "data_dir": "/abs/data"}}, value["raw"])

    def test_load_resolves_relative_data_dir_to_config_parent(self):
        config_file = self.root / "config.yaml"
        config_file.write_text(
            "server:\n  data_dir: ./data\n", encoding="utf-8"
        )
        with patch.object(config, "config_path", return_value=config_file):
            value = config.load()

        self.assertEqual((self.root / "data").resolve(), value["server"]["data_dir"])

    def test_load_malformed_yaml_falls_back_to_defaults(self):
        config_file = self.root / "config.yaml"
        config_file.write_text("server: [unclosed\n", encoding="utf-8")
        with patch.object(config, "config_path", return_value=config_file):
            value = config.load()

        server = value["server"]
        self.assertEqual(8765, server["port"])
        self.assertEqual("0.0.0.0", server["host"])

    def test_load_ignores_non_server_top_level_sections(self):
        config_file = self.root / "config.yaml"
        config_file.write_text(
            "other: true\nclient: {x: 1}\n", encoding="utf-8"
        )
        with patch.object(config, "config_path", return_value=config_file):
            value = config.load()

        # 没有 server 段 → 只保留默认值
        self.assertEqual("0.0.0.0", value["server"]["host"])
        self.assertEqual({"other": True, "client": {"x": 1}}, value["raw"])

    def test_load_defaults_tls_to_data_dir_tls_when_empty(self):
        """TLS 留空时默认指向 <data_dir>/tls/server.crt/.key（cert 生成位置）。"""
        config_file = self.root / "config.yaml"
        config_file.write_text(
            "server:\n  data_dir: ./data\n",
            encoding="utf-8",
        )
        with patch.object(config, "config_path", return_value=config_file):
            value = config.load()

        tls = value["server"]["tls"]
        self.assertEqual(
            str((self.root / "data" / "tls" / "server.crt").resolve()),
            tls["certfile"],
        )
        self.assertEqual(
            str((self.root / "data" / "tls" / "server.key").resolve()),
            tls["keyfile"],
        )

    def test_load_ignores_tls_paths_in_config(self):
        """config.yaml 不再配置证书路径：无论写什么，一律由 data_dir 推导缺省 TLS 位置。"""
        config_file = self.root / "config.yaml"
        config_file.write_text(
            "server:\n  data_dir: ./data\n  tls:\n    certfile: /custom/cert.crt\n    keyfile: /custom/key.key\n",
            encoding="utf-8",
        )
        with patch.object(config, "config_path", return_value=config_file):
            value = config.load()

        tls = value["server"]["tls"]
        self.assertEqual(
            str((self.root / "data" / "tls" / "server.crt").resolve()),
            tls["certfile"],
        )
        self.assertEqual(
            str((self.root / "data" / "tls" / "server.key").resolve()),
            tls["keyfile"],
        )


if __name__ == "__main__":
    unittest.main()