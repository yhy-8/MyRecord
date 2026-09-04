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
from client import identity as client_identity
from client import journal
from client import sync
from client.sync import SyncClient


def _tmp_dir(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def _client_settings(records_dir: Path, analysis_dir: Path) -> dict:
    """与 client.config.load 一致：目录键解析为绝对 Path。"""
    return {
        "client": {
            "server_url": "http://127.0.0.1:1",  # runner 会覆盖为真实地址
            "records_dir": records_dir,
            "analysis_dir": analysis_dir,
            "longpoll_timeout_seconds": 25,
        }
    }


class ClientSyncE2ETestBase(unittest.TestCase):
    def setUp(self):
        self._data = _tmp_dir("myrecord-srv-")
        self.store = Store(self._data / "state.json")

        # 单一共享链接凭证：所有客户端用同一个 token，设备身份由各端自报本机名区分。
        self.token = auth.new_token()
        self.store.register_device("e2e", self.token)
        self.token_a = self.token
        self.token_b = self.token
        self.device_a = "e2e-a"  # 客户端自报本机名
        self.device_b = "e2e-b"

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

    def _new_client(self, device, token, root: Path, server_url: str | None = None):
        """构造一个指向独立临时本地目录、使用真实令牌的 SyncClient。

        server_url 缺省指向 setUp 里启动的真实服务端；传入一个无人监听的端口
        即可模拟离线场景（离线队列会保留，等上线后冲刷）。
        """
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
                client_identity,
                "load",
                return_value={"token": token},
            ),
            patch.object(client_identity, "device_name", return_value=device),
        ]
        for p in patches:
            p.start()
        return SyncClient(server_url=server_url or self.base), patches

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
        try:
            client_a.pull()  # 与服务端做初始对账
            client_a.push_new(self._entry(self.device_a, 1, 1717200000, "第一条记录"))
            day = journal.day_path("2024-06-01").read_text(encoding="utf-8")
            self.assertIn("第一条记录", day)
            self.assertIn(f"{self.device_a}-1", day)
        finally:
            self._stop(pa)


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
            self.assertIn("myrecord-tombstone", b_content)  # 客户端 render 前缀

            client_b.send_pending()  # B 尝试推回（不应让 tombstone 条目复活）
            status = client_a.status()
            self.assertEqual(status["entry_count"], 0)
            self.assertEqual(status["tombstone_count"], 1)
        finally:
            self._stop(pa)
            self._stop(pb)


class OfflineQueueTest(ClientSyncE2ETestBase):
    """离线时写入先落本地并进 outbox；上线后 send_pending 冲刷到服务端。"""

    def test_offline_push_queues_then_flushes_when_online(self):
        root = _tmp_dir("cli-off-")
        entry = self._entry(self.device_a, 1, 1717200000, "离线写入")

        # 模拟 CLI 写记录：先本地落盘，再进离线队列（service unreachable → 保留）
        offline, po = self._new_client(
            self.device_a, self.token_a, root, server_url="http://127.0.0.1:1"
        )
        try:
            journal.append_record(entry)  # 本地写入永不回滚
            offline.push_new(entry)  # 进 outbox；推送失败 → 保留待续推
            day = journal.day_path("2024-06-01").read_text(encoding="utf-8")
            self.assertIn("离线写入", day)
            outbox_text = (root / "state" / "outbox.json").read_text(
                encoding="utf-8"
            )
            self.assertIn(self.device_a, outbox_text)
            self.assertIn("离线写入", outbox_text)
        finally:
            self._stop(po)

        # 上线：同一本地目录（共用同一 outbox），指向真实服务端后冲刷
        online, pa = self._new_client(
            self.device_b, self.token_b, root, server_url=self.base
        )
        try:
            online.send_pending()
            status = online.status()
            self.assertEqual(status["entry_count"], 1)
            self.assertNotIn(
                self.device_a,
                (root / "state" / "outbox.json").read_text(encoding="utf-8"),
            )
            day = journal.day_path("2024-06-01").read_text(encoding="utf-8")
            self.assertIn("离线写入", day)
        finally:
            self._stop(pa)


class FullSyncTest(ClientSyncE2ETestBase):
    """启动 / 手动 /sync 的完整同步：冲刷离线队列 + 拉取对账 + 同步报告。"""

    def test_full_sync_flushes_outbox_pulls_and_syncs_reports(self):
        root = _tmp_dir("cli-full-")
        # 写一条并离线（无人监听端口），进离线队列
        offline, po = self._new_client(
            self.device_a, self.token_a, root, server_url="http://127.0.0.1:1"
        )
        try:
            journal.append_record(self._entry(self.device_a, 1, 1717200000, "离线待推送"))
            offline.push_new(self._entry(self.device_a, 1, 1717200000, "离线待推送"))
            self.assertIn(
                self.device_a,
                (root / "state" / "outbox.json").read_text(encoding="utf-8"),
            )
        finally:
            self._stop(po)

        # 服务端暴露一个报告；线上客户端上线后 full_sync 一并处理
        samples = {"Weekly/2024-05-27_to_2024-06-02_auto.md": "# 周报\n"}
        self.httpd.list_reports = lambda kind: list(samples)
        self.httpd.read_report = lambda rel: samples.get(rel)

        online, pa = self._new_client(
            self.device_b, self.token_b, root, server_url=self.base
        )
        try:
            online.full_sync()
            # 离线队列被冲刷到服务端
            status = online.status()
            self.assertEqual(status["entry_count"], 1)
            self.assertNotIn(
                self.device_a,
                (root / "state" / "outbox.json").read_text(encoding="utf-8"),
            )
            # 本地日记对账到位
            day = journal.day_path("2024-06-01").read_text(encoding="utf-8")
            self.assertIn("离线待推送", day)
            # 报告同步到本地
            target = (
                root / "AnalysisReports" / "Weekly"
                / "2024-05-27_to_2024-06-02_auto.md"
            )
            self.assertTrue(target.exists())
            self.assertIn("周报", target.read_text(encoding="utf-8"))
        finally:
            self._stop(pa)


class FullSyncRecoveryTest(ClientSyncE2ETestBase):
    """回归：本地文件丢失但 state 游标未回退时，/sync 要能从云端版本0重建镜像。

    修复前 full_sync 走增量 pull（version=当前游标），游标已到当前值时拉不到内容，
    云端有数据却同步不下来；现在 full_sync 走 reconcile（version=0）完整对账。
    """

    def test_full_sync_recovers_missing_local_day_file(self):
        root = _tmp_dir("cli-rec-")
        # 服务端已有内容（某设备此前写入）
        self.store.append_entries(
            "MK8",
            [{"entry_id": "r1", "date": "2024-06-01", "ts": 1717200000, "tag": "", "text": "云端内容"}],
        )
        client, pat = self._new_client(self.device_a, self.token_a, root)
        try:
            client.full_sync()
            day_file = root / "Records" / "2024-06-01.md"
            self.assertTrue(day_file.exists())
            self.assertIn("云端内容", day_file.read_text(encoding="utf-8"))

            # 本地缓存丢失：删掉 day 文件，但 state 游标仍停留在当前版本
            day_file.unlink()
            client.full_sync()
            self.assertTrue(day_file.exists())
            self.assertIn("云端内容", day_file.read_text(encoding="utf-8"))
        finally:
            self._stop(pat)


class ProbeTest(ClientSyncE2ETestBase):
    """probe 区分「能否连到服务端」与「是否持有凭据」两个独立维度。"""

    def test_probe_connected_and_has_credentials(self):
        client, pat = self._new_client(self.device_a, self.token_a, _tmp_dir("cli-probe-"))
        try:
            result = client.probe()
            self.assertTrue(result["connected"])
            self.assertTrue(result["has_credentials"])
            self.assertFalse(result["error"])
        finally:
            self._stop(pat)

    def test_probe_disconnected_but_has_credentials(self):
        # 指向无人监听的端口：网络不可达，但凭据仍在——两维度独立。
        offline, pat = self._new_client(
            self.device_a, self.token_a, _tmp_dir("cli-probe-off-"),
            server_url="http://127.0.0.1:1",
        )
        try:
            result = offline.probe()
            self.assertFalse(result["connected"])
            self.assertTrue(result["has_credentials"])
            self.assertTrue(result["error"])
        finally:
            self._stop(pat)

    def test_probe_connected_but_no_credentials(self):
        # 服务端可达，但本地无凭据：连得上不代表有改数据权限。
        root = _tmp_dir("cli-probe-nocred-")
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
            patch.object(client_identity, "load", return_value={}),  # 无凭据
            patch.object(client_identity, "device_name", return_value=self.device_a),
        ]
        for p in patches:
            p.start()
        try:
            result = SyncClient(server_url=self.base).probe()
            self.assertTrue(result["connected"])
            self.assertFalse(result["has_credentials"])
        finally:
            for p in patches:
                p.stop()


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

    def test_client_refreshes_regenerated_report_to_newest(self):
        """同一时间段报告只保留最新生成：服务端重新生成（同路径、新内容）后客户端要覆盖旧副本。"""
        rel = "Weekly/2024-05-27_to_2024-06-02_auto.md"
        samples = {rel: "# 周报（v1 旧内容）\n"}
        self.httpd.list_reports = lambda kind: [rel]
        self.httpd.read_report = lambda r: samples.get(r)

        root = _tmp_dir("cli-refresh-")
        client_a, pat = self._new_client(self.device_a, self.token_a, root)
        try:
            # 第一次同步：拉取旧版本
            client_a.sync_reports()
            target = root / "AnalysisReports" / rel
            self.assertTrue(target.exists())
            self.assertIn("v1 旧内容", target.read_text(encoding="utf-8"))

            # 服务端重新生成了同一时段报告，内容更新
            samples[rel] = "# 周报（v2 最新）\n"

            # 第二次同步：应刷新为最新版本，而不是跳过旧副本
            client_a.sync_reports()
            self.assertIn("v2 最新", target.read_text(encoding="utf-8"))
            self.assertNotIn("v1 旧内容", target.read_text(encoding="utf-8"))
        finally:
            self._stop(pat)

    def test_client_does_not_rewrite_unchanged_report(self):
        """内容未变化的报告不重复写覆盖（避免无谓的磁盘写入）。"""
        rel = "Weekly/2024-05-27_to_2024-06-02_auto.md"
        content = "# 周报内容\n"
        samples = {rel: content}
        self.httpd.list_reports = lambda kind: [rel]
        self.httpd.read_report = lambda r: samples.get(r)

        root = _tmp_dir("cli-nochange-")
        client_a, pat = self._new_client(self.device_a, self.token_a, root)
        try:
            client_a.sync_reports()
            target = root / "AnalysisReports" / rel
            first_mtime = target.stat().st_mtime_ns
            client_a.sync_reports()
            self.assertEqual(first_mtime, target.stat().st_mtime_ns)
            self.assertIn("周报内容", target.read_text(encoding="utf-8"))
        finally:
            self._stop(pat)


class ReportPathTraversalGuardTest(unittest.TestCase):
    """客户端 sync_reports 必须拒绝写逃逸出 analysis_dir 的恶意相对路径（路径穿越兜底）。"""

    def test_sync_reports_ignores_escaping_rel_but_writes_valid_rel(self):
        root = _tmp_dir("cli-trav-")
        base = root / "AnalysisReports"
        outside = root / "outside_evil.md"
        base.mkdir(parents=True, exist_ok=True)
        cfg = {
            "client": {
                "records_dir": root / "Records",
                "analysis_dir": base,
                "server_url": "http://127.0.0.1:1",
                "longpoll_timeout_seconds": 25,
            }
        }
        with patch.object(client_config, "load", return_value=cfg):
            client = SyncClient(server_url="http://127.0.0.1:1")
            client._request = lambda *a, **k: {
                "reports": ["../outside_evil.md", "Weekly/ok.md"]
            }
            client._report_content = lambda rel: "# 内容\n"
            client.sync_reports()
        # 恶意相对路径不得写到 analysis_dir 之外
        self.assertFalse(outside.exists())
        # 合法相对路径仍正常写入
        ok = base / "Weekly" / "ok.md"
        self.assertTrue(ok.exists())
        self.assertIn("内容", ok.read_text(encoding="utf-8"))


class VerifyWarningSuppressionTest(unittest.TestCase):
    """verify 留空（跳过校验）时抑制 urllib3 的 InsecureRequestWarning，避免污染交互终端。"""

    def test_empty_verify_disables_insecure_warning_and_returns_false(self):
        with patch.object(client_config, "load", return_value={"client": {"verify": ""}}):
            with patch("urllib3.disable_warnings") as disable:
                client = SyncClient(server_url="https://localhost:8765")
                self.assertFalse(client._verify())
        disable.assert_called_once()

    def test_verify_path_returns_it_without_disabling_warning(self):
        with patch.object(client_config, "load", return_value={"client": {"verify": "/path/ca.crt"}}):
            with patch("urllib3.disable_warnings") as disable:
                client = SyncClient(server_url="https://localhost:8765")
                self.assertEqual("/path/ca.crt", client._verify())
        disable.assert_not_called()


class TombstonePlaceholderSyncTest(unittest.TestCase):
    """tombstone 占位符必须完整同步：即使客户端从未持有被删条目，也要写入占位符。"""

    def test_apply_delta_writes_placeholder_when_entry_never_seen(self):
        root = _tmp_dir("cli-tomb-")
        records = root / "Records"
        records.mkdir(parents=True, exist_ok=True)
        cfg = {
            "client": {
                "records_dir": records,
                "analysis_dir": root / "AnalysisReports",
                "server_url": "http://127.0.0.1:1",
                "longpoll_timeout_seconds": 25,
            }
        }
        with patch.object(client_config, "load", return_value=cfg):
            # 只有一条 tombstone，对应条目从未在本地出现过
            journal.apply_delta([], [{"entry_id": "never-had", "date": "2024-06-01"}])
        content = (records / "2024-06-01.md").read_text(encoding="utf-8")
        self.assertIn("myrecord-tombstone-id:never-had", content)

    def test_apply_delta_keeps_existing_placeholder_idempotent(self):
        """对账重复收到同一 tombstone 不应重复写入占位符。"""
        root = _tmp_dir("cli-tomb2-")
        records = root / "Records"
        records.mkdir(parents=True, exist_ok=True)
        cfg = {"client": {"records_dir": records, "analysis_dir": root / "A", "server_url": "http://x", "longpoll_timeout_seconds": 25}}
        with patch.object(client_config, "load", return_value=cfg):
            for _ in range(2):
                journal.apply_delta([], [{"entry_id": "x", "date": "2024-06-01"}])
        content = (records / "2024-06-01.md").read_text(encoding="utf-8")
        self.assertEqual(1, content.count("myrecord-tombstone-id:x"))

    def test_apply_delta_skips_entries_with_path_traversal_date(self):
        """date 含 `../` 的条目不得写出 Records 之外（防御性跳过）。"""
        root = _tmp_dir("cli-esc-")
        records = root / "Records"
        records.mkdir(parents=True, exist_ok=True)
        outside = root / "escape.md"
        cfg = {"client": {"records_dir": records, "analysis_dir": root / "A", "server_url": "http://x", "longpoll_timeout_seconds": 25}}
        with patch.object(client_config, "load", return_value=cfg):
            journal.apply_delta(
                [{"entry_id": "e1", "date": "../escape", "ts": 1, "tag": "", "text": "x"}],
                [],
            )
        self.assertFalse(outside.exists())
        self.assertEqual([], list(records.glob("*.md")))  # 未写入任何日期文件


if __name__ == "__main__":
    unittest.main()