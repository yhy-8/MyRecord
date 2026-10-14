"""服务端 TLS 直连的健壮性：半开连接不得卡死 accept 主循环。

回归背景：serve() 曾把监听 socket 用 SSLContext.wrap_socket 包裹且默认
do_handshake_on_connect=True，SSLSocket.accept() 会在 **accept 主循环线程**里完成
TLS 握手。任一客户端只建 TCP 不握手（睡眠/掉线/NAT 超时/端口扫描）就会把主循环
卡死，服务端对新连接表现为“无法连接”，直到进程重启。修复后握手延迟到工作线程，
并有超时兜底。
"""

import socket
import ssl
import tempfile
import threading
import time
import unittest
import urllib3
from pathlib import Path

import requests

from server.hub import auth
from server.hub.server import serve
from server.hub.store import Store
from server.main import _generate_cert


class TlsStalledHandshakeTest(unittest.TestCase):
    def setUp(self):
        # TLS/证书生成是服务端职责，依赖 cryptography（server/requirements.txt）；
        # 客户端构建 CI 只装 client/requirements.txt，缺该依赖时按仓库既有惯例
        # 优雅跳过，而不是让整组用例硬出错。
        try:
            from cryptography import x509  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("缺少 cryptography：`pip install cryptography`")
        self._dir = Path(tempfile.mkdtemp(prefix="myrecord-tls-"))
        certfile, keyfile = _generate_cert(self._dir)
        self.certfile = certfile
        self.store = Store(self._dir / "state.json")
        self.token = auth.new_token()
        self.store.register_device("tls-test", self.token)

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile, keyfile)
        self.httpd = serve(self.store, "127.0.0.1", 0, ssl_context=context)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.stall = None

    def tearDown(self):
        if self.stall is not None:
            self.stall.close()
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_stalled_tcp_connection_does_not_block_valid_tls_request(self):
        # 半开连接：只建 TCP、不发 TLS ClientHello，模拟睡眠/掉线的客户端。
        self.stall = socket.create_connection(("127.0.0.1", self.port))
        time.sleep(0.3)

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        resp = requests.get(
            f"https://127.0.0.1:{self.port}/api/health", verify=False, timeout=5
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
