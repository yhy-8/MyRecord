"""P2/P3 客户端同步 e2e 测试：真实 Store + HTTP 中枢 + 薄客户端 SyncClient。

覆盖：
- 写后即 push（写进离线队列并立即推送）→ 服务端合并、客户端对账
- 双设备扇出：A 写入，B 通过拉取收到
- tombstone 防复活：A 在线删除，B 离线时本地已有该条，上线拉取后被移除且不推回
- 报告同步：服务端暴露的报告被客户端拉到本地 AnalysisReports
"""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from server.hub import auth
from server.hub.store import Store
from server.hub.server import serve

from client import config as client_config
from client import credentials as client_credentials
from client import journal
from client import sync
from client.sync import SyncClient


def _tmp_dir(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def _client_settings(records_dir: Path, analysis_dir: Path) -> dict:
    """与 client.config.load 一致：三个目录键解析为绝对 Path。"""
    return {
        "client": {
            "server_url": "http://127.0.0.1:1",  # runner 会覆盖为真实地址
            "records_dir": records_dir,
            "analysis_dir": analysis_dir,
            "log_dir": records_dir.parent / "Log",
            "poll_interval_seconds": 60,
            "longpoll_timeout_seconds": 25,
        }
    }


class ClientSyncE2ETestBase(unittest.TestCase):
    def setUp(self):
        self._data = _tmp_dir("agentrecord-srv-")
        self.store = Store(self._data / "state.json")

        self.token_a = auth.new_token()
        self.device_a = self.store.register_device("e2e-a", self.token_a)
        self.token_b = auth.new_token()
        self.device_b = self.store.register_device("e2e-b", self.token_b)

        self.httpd = serve(self.store, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _new_client(self, device, token, root: Path):
        """构造一个指向独立临时本地目录、使用真实令牌的 SyncClient。"""
        records = root / "Records"
        analysis = root / "AnalysisReports"
        state = root / "state"
        records.mkdir(parents=True, exist_ok=True)
        analysis.mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True)
        patches = [
            patch.object(sync, "_state_path", return_value=state / "state.json"),
            patch.object(sync, "_outbox_path", return_value=state / "outbox.json"),
            patch.object(
                client_config, "load", return_value=_client_settings(records, analysis)
            ),
            patch.object(
                client_credentials,
                "require",
                return_value={"device_id": device, "token": token},
            ),
        ]
        for p in patches:
            p.start()
        return SyncClient(server_url=self.base), patches

    def _stop(self, patches) -> None:
        for p in patches:
            p.stop()

    def _entry(self, device, seq, ts, text, date="2024-06-01"):
        return {
            "entry_id": f"{device}-{seq}",
            "device_id": device,
            "date": date,
            "ts": ts,
            "tag": "",
            "text": text,
        }


class WritePushReconcileTest(ClientSyncE2ETestBase):
    """A 写一条 → 服务端合并 → A 本地对账。"""

    def test_push_and_reconcile_back_to_local(self):
        client_a, pa = self._new_client(self.device_a, self.token_a, _tmp_dir("cli-a-"))
        client_b, pb = self._new_client(self.device_b, self.token_b, _tmp_dir("cli-b-"))
        try:
            client_b.pull()  # 与 server_hub 同样的初始对账
            client_a.push_new(self._entry(self.device_a, 1, 1717200000, "第一条记录"))
            day = journal.day_path("2024-06-01").read_text(encoding="utf-8")
            self.assertIn("第一条记录", day)
            self.assertIn(f"{self.device_a}-1", day)
        finally:
            self._stop(pa)
            self._stop(pb)


class FanoutBetweenDevicesTest(ClientSyncE2ETestBase):
    """A 写入 → B 拉取收到同一条目（双设备扇出）。"""

    def test_device_b_receives_via_pull(self):
        root_a = _tmp_dir("cli-a-")
        root_b = _tmp_dir("cli-b-")
        client_a, pa = self._new_client(self.device_a, self.token_a, root_a)
        client_b, pb = self._new_client(self.device_b, self.token_b, root_b)
        try:
            client_a.push_new(self._entry(self.device_a, 1, 1717200000, "来自设备A"))
            client_b.pull()
            content = (root_b / "Records" / "2024-06-01.md").read_text(encoding="utf-8")
            self.assertIn("来自设备A", content)
            self.assertIn(f"{self.device_a}-1", content)
        finally:
            self._stop(pa)
            self._stop(pb)


class TombstoneAntiResurrectionTest(ClientSyncE2ETestBase):
    """A 在线删除当天最新一条；B 离线时本地已有该条，上线拉取后被移除且不推回。"""

    def test_deleted_entry_not_resurrected_on_b(self):
        root_a = _tmp_dir("cli-a-")
        root_b = _tmp_dir("cli-b-")
        client_a, pa = self._new_client(self.device_a, self.token_a, root_a)
        client_b, pb = self._new_client(self.device_b, self.token_b, root_b)
        try:
            client_a.push_new(self._entry(self.device_a, 1, 1717200060, "将被删除"))
            client_b.pull()  # B 已同步到本地（模拟离线前）
            b_file = root_b / "Records" / "2024-06-01.md"
            self.assertIn("将被删除", b_file.read_text(encoding="utf-8"))

            # A 在线删除当天最新一条 → 服务端 tombstone
            deleted = client_a.delete_latest("2024-06-01")
            self.assertEqual(deleted, f"{self.device_a}-1")

            # B 上线拉取 → 本地移除该条，且不会把已删条目推回服务端
            client_b.pull()
            b_content = b_file.read_text(encoding="utf-8")
            self.assertNotIn("将被删除", b_content)
            self.assertIn("agentrecord-tombstone", b_content)  # 客户端 render 前缀

            client_b.send_pending()  # B 尝试推回（不应让 tombstone 条目复活）
            status = client_a.status()
            self.assertEqual(status["entry_count"], 0)
            self.assertEqual(status["tombstone_count"], 1)
        finally:
            self._stop(pa)
            self._stop(pb)


class ReportSyncTest(ClientSyncE2ETestBase):
    """服务端暴露的报告被客户端拉到本地 AnalysisReports。"""

    def test_client_pulls_report(self):
        samples = {"Weekly/2024-05-27_to_2024-06-02_auto.md": "# 周报内容\n"}
        self.httpd.list_reports = lambda kind: list(samples)
        self.httpd.read_report = lambda rel: samples.get(rel)

        root = _tmp_dir("cli-")
        client_a, pat = self._new_client(self.device_a, self.token_a, root)
        try:
            client_a.sync_reports()
            target = (
                root / "AnalysisReports" / "Weekly"
                / "2024-05-27_to_2024-06-02_auto.md"
            )
            self.assertTrue(target.exists())
            self.assertIn("周报内容", target.read_text(encoding="utf-8"))
        finally:
            self._stop(pat)


if __name__ == "__main__":
    unittest.main()