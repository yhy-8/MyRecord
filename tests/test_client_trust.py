"""客户端 TOFU 信任（client/trust.py）测试。

覆盖：首次连接确认后落盘并改配置、拒绝则退出、证书一致直接通过、不一致时覆盖/拒绝、
网络不可达时的取舍、证书详情展示，以及证书固定适配器（只认证书、不校验主机名）。
"""

import http.server
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from client import config as client_config
from client import trust
from client.sync import SyncClient

_HAS_OPENSSL = shutil.which("openssl") is not None


def _make_cert(cn: str, directory: str) -> Path:
    key = Path(directory) / f"{cn}.key"
    crt = Path(directory) / f"{cn}.crt"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(crt), "-days", "1", "-subj", f"/CN={cn}",
        ],
        capture_output=True,
        check=True,
    )
    return crt


@unittest.skipUnless(_HAS_OPENSSL, "需要 openssl 生成测试证书")
class TrustFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="trust-cert-")
        cls.pem_a = _make_cert("server-a", cls.tmpdir).read_text(encoding="utf-8")
        cls.pem_b = _make_cert("server-b", cls.tmpdir).read_text(encoding="utf-8")

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="trust-"))
        self.cert = self.root / "server.crt"
        self.config_path = self.root / "config.yaml"
        self._write_config('client:\n  server_url: "https://x:8765"\n  verify: ""\n')
        self._patches = [
            patch.object(trust, "cert_path", return_value=self.cert),
            patch.object(client_config, "config_path", return_value=self.config_path),
        ]
        for item in self._patches:
            item.start()

    def tearDown(self):
        for item in self._patches:
            item.stop()

    def _write_config(self, text: str) -> None:
        self.config_path.write_text(text, encoding="utf-8")

    @staticmethod
    def _answers(*values):
        iterator = iter(values)
        return lambda prompt="": next(iterator)

    def test_first_use_accept_saves_cert_and_points_config(self):
        with patch.object(trust, "fetch_peer_pem", return_value=self.pem_a):
            ok = trust.ensure_trusted("https://x:8765", input_func=self._answers("y"))
        self.assertTrue(ok)
        self.assertEqual(self.pem_a, self.cert.read_text(encoding="utf-8"))
        self.assertIn('verify: "./server.crt"', self.config_path.read_text(encoding="utf-8"))

    def test_first_use_refuse_saves_nothing(self):
        with patch.object(trust, "fetch_peer_pem", return_value=self.pem_a):
            ok = trust.ensure_trusted("https://x:8765", input_func=self._answers("n"))
        self.assertFalse(ok)
        self.assertFalse(self.cert.exists())
        self.assertIn('verify: ""', self.config_path.read_text(encoding="utf-8"))

    def test_matching_pinned_cert_passes_without_prompt(self):
        self.cert.write_text(self.pem_a, encoding="utf-8")
        self._write_config('client:\n  verify: "./server.crt"\n')

        def boom(prompt=""):
            raise AssertionError("证书一致时不应再询问用户")

        with patch.object(trust, "fetch_peer_pem", return_value=self.pem_a):
            self.assertTrue(trust.ensure_trusted("https://x:8765", input_func=boom))

    def test_mismatch_overwrite_on_confirm(self):
        self.cert.write_text(self.pem_a, encoding="utf-8")
        self._write_config('client:\n  verify: "./server.crt"\n')
        with patch.object(trust, "fetch_peer_pem", return_value=self.pem_b):
            ok = trust.ensure_trusted("https://x:8765", input_func=self._answers("y"))
        self.assertTrue(ok)
        self.assertEqual(self.pem_b, self.cert.read_text(encoding="utf-8"))

    def test_mismatch_refuse_keeps_old_cert(self):
        self.cert.write_text(self.pem_a, encoding="utf-8")
        self._write_config('client:\n  verify: "./server.crt"\n')
        with patch.object(trust, "fetch_peer_pem", return_value=self.pem_b):
            ok = trust.ensure_trusted("https://x:8765", input_func=self._answers("n"))
        self.assertFalse(ok)
        self.assertEqual(self.pem_a, self.cert.read_text(encoding="utf-8"))

    def test_unreachable_with_pinned_cert_proceeds(self):
        self.cert.write_text(self.pem_a, encoding="utf-8")
        with patch.object(trust, "fetch_peer_pem", side_effect=trust.TrustError("down")):
            self.assertTrue(trust.ensure_trusted("https://x:8765", input_func=self._answers("n")))

    def test_unreachable_without_pinned_cert_fails(self):
        with patch.object(trust, "fetch_peer_pem", side_effect=trust.TrustError("down")):
            self.assertFalse(trust.ensure_trusted("https://x:8765", input_func=self._answers("y")))

    def test_non_https_is_rejected(self):
        with self.assertRaises(trust.TrustError):
            trust._fetch_der("http://x:1", 1.0)

    def test_describe_contains_subject_and_fingerprint(self):
        text = trust.describe(self.pem_a)
        self.assertIn("CN=server-a", text)
        self.assertIn(trust.fingerprint(self.pem_a), text)

    def test_set_verify_preserves_comment(self):
        self._write_config('client:\n  verify: ""  # keep me\n')
        client_config.set_verify("./server.crt")
        text = self.config_path.read_text(encoding="utf-8")
        self.assertIn('verify: "./server.crt"', text)
        self.assertIn("# keep me", text)

    def test_sync_client_mounts_pinned_adapter_when_verify_set(self):
        self.cert.write_text(self.pem_a, encoding="utf-8")
        with patch.object(
            client_config, "load", return_value={"client": {"verify": str(self.cert)}}
        ):
            session = SyncClient("https://x:8765")._client()
        self.assertIsInstance(session.get_adapter("https://x:8765"), trust.PinnedHTTPAdapter)

    def test_sync_client_disables_verify_when_empty_and_no_pinned_cert(self):
        with patch.object(
            client_config, "load", return_value={"client": {"verify": ""}}
        ), patch.object(trust, "cert_path", return_value=self.root / "no-such.crt"), patch(
            "urllib3.disable_warnings"
        ) as disable:
            session = SyncClient("https://x:8765")._client()
        self.assertFalse(session.verify)
        disable.assert_called_once()

    def test_sync_client_uses_pinned_cert_fallback_when_verify_empty(self):
        """verify 被清空但 client/server.crt 仍在时，仍应固定校验而非静默降级。"""
        self.cert.write_text(self.pem_a, encoding="utf-8")
        with patch.object(
            client_config, "load", return_value={"client": {"verify": ""}}
        ), patch.object(trust, "cert_path", return_value=self.cert):
            session = SyncClient("https://x:8765")._client()
        self.assertIsInstance(session.get_adapter("https://x:8765"), trust.PinnedHTTPAdapter)


@unittest.skipUnless(_HAS_OPENSSL, "需要 openssl 生成测试证书")
class PinnedAdapterTests(unittest.TestCase):
    """证书固定适配器：只认固定证书、不校验主机名，且能拒绝别的证书。"""

    def test_accepts_matching_cert_and_rejects_other(self):
        directory = tempfile.mkdtemp(prefix="pin-")
        cert = _make_cert("local", directory)
        key = cert.with_suffix(".key")
        other = _make_cert("evil", directory)

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"https://127.0.0.1:{server.server_address[1]}/"
        try:
            session = requests.Session()
            session.mount("https://", trust.PinnedHTTPAdapter(str(cert)))
            self.assertEqual(200, session.get(url, timeout=5).status_code)

            wrong = requests.Session()
            wrong.mount("https://", trust.PinnedHTTPAdapter(str(other)))
            with self.assertRaises(requests.exceptions.SSLError):
                wrong.get(url, timeout=5)
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
