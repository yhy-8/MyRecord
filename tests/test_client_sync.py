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


# 方案 B：唯一可写窗口 = “今天”（UTC+8）。测试统一用它替代硬编码日期。
_TODAY = journal.today_utc8()


def _tmp_dir(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def _client_settings(records_dir: Path, analysis_dir: Path) -> dict:
    """与 client.config.load 一致：目录键解析为绝对 Path。"""
    return {
        "client": {
            "server_url": "http://127.0.0.1:1",  # runner 会覆盖为真实地址
            "records_dir": records_dir,
            "analysis_dir": analysis_dir,
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

    def _entry(self, device, seq, ts, text, date=_TODAY):
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
            day = journal.day_path(_TODAY).read_text(encoding="utf-8")
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
            content = (root_b / "Records" / f"{_TODAY}.md").read_text(encoding="utf-8")
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
            b_file = root_b / "Records" / f"{_TODAY}.md"
            self.assertIn("将被删除", b_file.read_text(encoding="utf-8"))

            # A 在线删除当天最新一条 → 服务端 tombstone
            deleted = client_a.delete_latest(_TODAY)
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


class OutboxRepushResurrectionTest(ClientSyncE2ETestBase):
    """回归：删除后，客户端 outbox 重推已删条目不得让它在服务端复活。

    场景：推送响应丢失导致条目仍留在 outbox；随后该条被删除（服务端已写入 tombstone）。
    客户端上线重推 outbox，服务端的 append_entries 若只去重 entries，会把墓碑里的条目
    重新加入 entries，导致删除被回滚、在各端复活。
    """

    def test_tombstoned_entry_not_resurrected_by_outbox_repush(self):
        import json

        root_a = _tmp_dir("cli-repush-a-")
        root_b = _tmp_dir("cli-repush-b-")
        client_a, pa = self._new_client(self.device_a, self.token_a, root_a)
        try:
            # A 写入一条并推到服务端
            client_a.push_new(self._entry(self.device_a, 1, 1717200060, "将被删"))
            self.assertEqual(len(self.store.data["entries"]), 1)

            # A 在线删除当天最新一条 → 服务端 tombstone，条目从 entries 移除
            deleted = client_a.delete_latest(_TODAY)
            self.assertEqual(deleted, f"{self.device_a}-1")
            self.assertEqual(self.store.data["entries"], {})
            self.assertEqual(len(self.store.data["tombstones"]), 1)

            # 模拟另一台客户端 B：其 outbox 里仍残留这条（推送响应丢失未清）
            client_b, pb = self._new_client(
                self.device_b, self.token_b, root_b, server_url=self.base
            )
            try:
                outbox_path = root_b / "state" / "outbox.json"
                outbox_path.write_text(
                    json.dumps({"entries": [self._entry(self.device_a, 1, 1717200060, "将被删")]}),
                    encoding="utf-8",
                )

                # B 上线冲刷 outbox → 服务端不得复活该条目
                client_b.send_pending()
                self.assertNotIn(f"{self.device_a}-1", self.store.data["entries"])
                self.assertEqual(len(self.store.data["tombstones"]), 1)
                # B 的 outbox 应被清空（视为已接受，避免永久重试）
                self.assertEqual(
                    json.loads(outbox_path.read_text(encoding="utf-8"))["entries"], []
                )
            finally:
                self._stop(pb)
        finally:
            self._stop(pa)


class MultiClientConsistencyTest(ClientSyncE2ETestBase):
    """多端同步一致性：多设备离线记录乱序重建、删除跨端展开且不复活、客户端与服务端渲染严格一致。"""

    def test_multi_device_offline_recording_converges_to_time_order(self):
        """多设备离线记录（时间乱序入库）→ 各端上线全量对账后本地镜像按时间排序。"""
        root_a = _tmp_dir("mc-a-")
        root_b = _tmp_dir("mc-b-")
        # 模拟离线批量推送：A 先连（晚 time 先入），B 后连（早 time 后入）→ 服务端 v 序乱、时间序不乱
        self.store.append_entries(self.device_a, [
            {"entry_id": "late", "date": _TODAY, "ts": 1717200120, "tag": "", "text": "A-晚"},
        ])
        self.store.append_entries(self.device_b, [
            {"entry_id": "early", "date": _TODAY, "ts": 1717200000, "tag": "", "text": "B-早"},
        ])
        # 各客户端依次 reconcile 到自己的本地镜像（测试基底的 config 补丁不支持多个并发客户端共享，故逐个进行）
        ca, pa = self._new_client(self.device_a, self.token_a, root_a)
        try:
            ca.full_sync()
        finally:
            self._stop(pa)
        cb, pb = self._new_client(self.device_b, self.token_b, root_b)
        try:
            cb.full_sync()
        finally:
            self._stop(pb)
        ca_content = (root_a / "Records" / f"{_TODAY}.md").read_text(encoding="utf-8")
        cb_content = (root_b / "Records" / f"{_TODAY}.md").read_text(encoding="utf-8")
        # 时间较早的 B-早 应排在时间较晚的 A-晚 之前（即使服务端按 v 序 push、时序乱）
        self.assertLess(ca_content.index("B-早"), ca_content.index("A-晚"))
        self.assertLess(cb_content.index("B-早"), cb_content.index("A-晚"))

    def test_client_matches_server_render_after_reconcile(self):
        """同一份权威数据：客户端重建后的每日文件与服务端渲染严格一致（含墓碑插回原位）。"""
        root = _tmp_dir("mc-consist-")
        self.store.append_entries(self.device_a, [
            {"entry_id": "x1", "date": _TODAY, "ts": 1717200000, "tag": "", "text": "早"},
            {"entry_id": "x3", "date": _TODAY, "ts": 1717200120, "tag": "", "text": "晚"},
        ])
        self.store.append_entries(self.device_b, [
            {"entry_id": "x2", "date": _TODAY, "ts": 1717200060, "tag": "", "text": "中"},
        ])
        self.store.tombstone("x2", self.device_b)  # 中间的 x2 被删 → 墓碑应插回其原位置
        client, pat = self._new_client(self.device_a, self.token_a, root)
        try:
            client.full_sync()
            client_content = (root / "Records" / f"{_TODAY}.md").read_text(encoding="utf-8")
            srv_records = _tmp_dir("mc-srv-") / "Records"
            self.store.render_records(srv_records, srv_records.parent / "Trash")
            server_content = (srv_records / f"{_TODAY}.md").read_text(encoding="utf-8")
            # 客户端镜像与服务端渲染逐字节一致：同格式、同时序、墓碑同位置
            self.assertEqual(client_content, server_content)
            self.assertLess(client_content.index("早"), client_content.index("myrecord-tombstone-time:x2"))
            self.assertLess(client_content.index("myrecord-tombstone-time:x2"), client_content.index("晚"))
        finally:
            self._stop(pat)

    def test_delete_fans_out_and_never_resurrects_across_three_devices(self):
        """A 删除当天最新一条；B、C（离线时本地已有该条）上线都不复活，且三端与服务端一致。"""
        roots = {d: _tmp_dir(f"mc3-{d}-") for d in ("a", "b", "c")}
        entry = self._entry(self.device_a, 1, 1717200060, "将被删")

        # A 写入并落盘、推送到服务端
        ca, pa = self._new_client(self.device_a, self.token_a, roots["a"])
        try:
            journal.append_record(entry)  # 本地落盘
            ca.push_new(entry)            # 推送到服务端
        finally:
            self._stop(pa)

        # B、C 各自同步到本地（离线前均持有该条）
        _devices = {"a": self.device_a, "b": self.device_b, "c": "e2e-c"}
        for dev in ("b", "c"):
            c, p = self._new_client(_devices[dev], self.token, roots[dev])
            try:
                c.full_sync()
            finally:
                self._stop(p)
        for dev in ("a", "b", "c"):
            content = (roots[dev] / "Records" / f"{_TODAY}.md").read_text(encoding="utf-8")
            self.assertIn("将被删", content)

        # A 在线删除当天最新一条
        ca, pa = self._new_client(self.device_a, self.token_a, roots["a"])
        try:
            deleted = ca.delete_latest(_TODAY)
        finally:
            self._stop(pa)
        self.assertEqual(deleted, f"{self.device_a}-1")
        self.assertEqual(len(self.store.data["tombstones"]), 1)

        # B、C 上线拉取 → 本地移除且不复活
        for dev in ("b", "c"):
            c, p = self._new_client(_devices[dev], self.token, roots[dev])
            try:
                c.full_sync()
            finally:
                self._stop(p)

        # 三端 + 服务端一致：已被删、无复活
        self.assertEqual(self.store.data["entries"], {})
        self.assertEqual(len(self.store.data["tombstones"]), 1)
        for dev in ("a", "b", "c"):
            content = (roots[dev] / "Records" / f"{_TODAY}.md").read_text(encoding="utf-8")
            self.assertNotIn("将被删", content)


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
            day = journal.day_path(_TODAY).read_text(encoding="utf-8")
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
            day = journal.day_path(_TODAY).read_text(encoding="utf-8")
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
            day = journal.day_path(_TODAY).read_text(encoding="utf-8")
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

    def test_full_sync_rebuilds_local_file_in_time_order(self):
        """多端离线记录后全量对账：本地镜像按时间排序重建，而非按推送顺序（v）。

        回归：多端离线写入后各端批量上传，服务端按推送顺序（v）入库，条目时间反而
        乱序；全量对账（reconcile）须把本地文件重建为与服务端渲染一致的时间有序结构。
        """
        root = _tmp_dir("cli-order-")
        # 服务端先有按推送顺序（v）到达但时间乱序的条目（late 先入、early 后入）
        self.store.append_entries("a", [
            {"entry_id": "late", "date": _TODAY, "ts": 1717200120, "tag": "", "text": "晚"},
            {"entry_id": "early", "date": _TODAY, "ts": 1717200000, "tag": "", "text": "早"},
        ])
        client, pat = self._new_client(self.device_a, self.token_a, root)
        try:
            client.full_sync()
            content = (root / "Records" / f"{_TODAY}.md").read_text(encoding="utf-8")
            # 早(ts=1717200000) 应排在 晚(ts=1717200120) 之前
            self.assertLess(content.index("早"), content.index("晚"))
        finally:
            self._stop(pat)


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
            [{"entry_id": "r1", "date": _TODAY, "ts": 1717200000, "tag": "", "text": "云端内容"}],
        )
        client, pat = self._new_client(self.device_a, self.token_a, root)
        try:
            client.full_sync()
            day_file = root / "Records" / f"{_TODAY}.md"
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
            }
        }
        with patch.object(client_config, "load", return_value=cfg):
            client = SyncClient(server_url="http://127.0.0.1:1")
            client._request = lambda *a, **k: {
                "files": [
                    {"rel": "../outside_evil.md", "sha256": "a" * 64},
                    {"rel": "Weekly/ok.md", "sha256": "b" * 64},
                ]
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
            }
        }
        with patch.object(client_config, "load", return_value=cfg):
            # 只有一条 tombstone，对应条目从未在本地出现过
            journal.apply_delta([], [{"entry_id": "never-had", "date": _TODAY}])
        content = (records / f"{_TODAY}.md").read_text(encoding="utf-8")
        self.assertIn("myrecord-tombstone-time:never-had", content)

    def test_apply_delta_keeps_existing_placeholder_idempotent(self):
        """对账重复收到同一 tombstone 不应重复写入占位符。"""
        root = _tmp_dir("cli-tomb2-")
        records = root / "Records"
        records.mkdir(parents=True, exist_ok=True)
        cfg = {"client": {"records_dir": records, "analysis_dir": root / "A", "server_url": "http://x"}}
        with patch.object(client_config, "load", return_value=cfg):
            for _ in range(2):
                journal.apply_delta([], [{"entry_id": "x", "date": _TODAY}])
        content = (records / f"{_TODAY}.md").read_text(encoding="utf-8")
        self.assertEqual(1, content.count("myrecord-tombstone-time:x"))

    def test_apply_delta_skips_entries_with_path_traversal_date(self):
        """date 含 `../` 的条目不得写出 Records 之外（防御性跳过）。"""
        root = _tmp_dir("cli-esc-")
        records = root / "Records"
        records.mkdir(parents=True, exist_ok=True)
        outside = root / "escape.md"
        cfg = {"client": {"records_dir": records, "analysis_dir": root / "A", "server_url": "http://x"}}
        with patch.object(client_config, "load", return_value=cfg):
            journal.apply_delta(
                [{"entry_id": "e1", "date": "../escape", "ts": 1, "tag": "", "text": "x"}],
                [],
            )
        self.assertFalse(outside.exists())
        self.assertEqual([], list(records.glob("*.md")))  # 未写入任何日期文件


class SubSecondOrderingTest(unittest.TestCase):
    """同秒多条记录：毫秒级 ts 决定先后，而非被 entry_id 哈希打乱（客户端镜像）。"""

    def test_render_orders_same_second_by_ms(self):
        from client import render as client_render

        # 同一秒(1717200000)但毫秒不同；entry_id 字典序与 ms 顺序相反
        entries = [
            {"entry_id": "z-2", "date": _TODAY, "ts": 1717200000123, "tag": "", "text": "1"},
            {"entry_id": "a-1", "date": _TODAY, "ts": 1717200000246, "tag": "", "text": "2"},
        ]
        text = client_render.render_day_file(_TODAY, entries)
        # 按 ms 顺序（先 1 后 2），而非按 entry_id 字典序（a-1 本应在前）
        self.assertLess(
            text.index("<!-- myrecord-time:z-2 -->"),
            text.index("<!-- myrecord-time:a-1 -->"),
        )

    def test_fmt_hhmm_from_ms(self):
        from client import render as client_render

        # 毫秒时间戳换算为正确 HH:MM
        self.assertTrue(client_render._fmt_hhmm(1717200000246).endswith(":00"))

    def test_fmt_hhmm_uses_utc8(self):
        from client import render as client_render

        # epoch 1717200000000ms = 2024-01-01 00:00 UTC = 08:00 UTC+8（客户端镜像同样固定 UTC+8）
        self.assertEqual(client_render._fmt_hhmm(1717200000000), "08:00")
        self.assertEqual(client_render._fmt_hhmm(1717200000123), "08:00")

    def test_entry_marker_carries_timestamp_as_id(self):
        from client import render as client_render

        # id 就是毫秒时间戳（自描述时间），标签为 myrecord-time
        entry = {"entry_id": "1717200000123", "date": _TODAY, "ts": 1717200000123, "tag": "", "text": "x"}
        block = client_render.entry_block(entry)
        self.assertIn("<!-- myrecord-time:1717200000123 -->", block)


class IncrementalApplyDeltaOrderTest(unittest.TestCase):
    """回归：增量对账（apply_delta）也要保持当天文件按 (ts, entry_id) 时间有序。

    多端离线记录、服务端按推送顺序（v）入库时，条目/墓碑到达顺序与时间序可能不一致；
    若 apply_delta 只是把新块追加到文件末尾，本地镜像就会乱序（复现：真实设备 t2 的
    2026-09-06 内容按推送序排列而非时间序）。必须以块为单位合并重排，与全量重建
    （rebuild_records）得到一致的时间有序镜像。
    """

    def _entry(self, entry_id, ts, text):
        return {
            "entry_id": entry_id,
            "device_id": "MK8",
            "date": _TODAY,
            "ts": ts,
            "tag": "",
            "text": text,
        }

    def test_apply_delta_keeps_day_time_sorted_when_fanout_arrives_earlier(self):
        """本地已有晚时间条目；扇出增量带来更早时间条目 → 应插到前面，而非追加到末尾。"""
        root = _tmp_dir("cli-incr-order-")
        records = root / "Records"
        records.mkdir(parents=True, exist_ok=True)
        cfg = {
            "client": {
                "records_dir": records,
                "analysis_dir": root / "A",
                "server_url": "http://x",
            }
        }
        with patch.object(client_config, "load", return_value=cfg):
            journal.rebuild_records(
                [
                    self._entry("436841", 436841, "我的刀盾"),
                    self._entry("7507454", 7507454, "?"),
                ],
                [],
            )
            # 服务端扇出的增量（时间更早）经 apply_delta 合并
            journal.apply_delta(
                [
                    self._entry("426853", 426853, "2"),
                    self._entry("431749", 431749, "姑姑嘎嘎"),
                ],
                [],
            )
        content = (records / f"{_TODAY}.md").read_text(encoding="utf-8")
        order = {eid: content.index(f"<!-- myrecord-time:{eid} -->") for eid in ("426853", "431749", "436841", "7507454")}
        # 按 (ts, entry_id) 时间有序：早的两个在晚的两个之前
        self.assertLess(order["426853"], order["436841"])
        self.assertLess(order["431749"], order["436841"])
        self.assertLess(order["436841"], order["7507454"])
        self.assertLess(content.index("2"), content.index("我的刀盾"))

    def test_apply_delta_idempotent_on_repeat(self):
        """重复 apply_delta 同一批增量：不重复追加，仍时间有序。"""
        root = _tmp_dir("cli-incr-idem-")
        records = root / "Records"
        records.mkdir(parents=True, exist_ok=True)
        cfg = {
            "client": {
                "records_dir": records,
                "analysis_dir": root / "A",
                "server_url": "http://x",
            }
        }
        with patch.object(client_config, "load", return_value=cfg):
            entries = [
                self._entry("426853", 426853, "2"),
                self._entry("436841", 436841, "我的刀盾"),
            ]
            journal.apply_delta(entries, [])
            journal.apply_delta(entries, [])  # 重复
        content = (records / f"{_TODAY}.md").read_text(encoding="utf-8")
        self.assertEqual(1, content.count("myrecord-time:426853 -->"))
        self.assertEqual(1, content.count("myrecord-time:436841 -->"))

    def test_apply_delta_orders_bare_imported_entries_by_derived_time(self):
        """回归：导入的裸记录（非时间戳 id，ts 由 HH:MM 推导、分钟对齐）也按时间次序排列。

        裸记录之间 ts 常重复，(ts, entry_id) 排序里 entry_id 才是真实次序键；客户端对
        已有裸记录块重排时须还原其真实 ts（而非一刀切排到最前），否则与服务端渲染不一致。
        """
        root = _tmp_dir("cli-incr-bare-")
        records = root / "Records"
        records.mkdir(parents=True, exist_ok=True)
        cfg = {
            "client": {
                "records_dir": records,
                "analysis_dir": root / "A",
                "server_url": "http://x",
            }
        }
        with patch.object(client_config, "load", return_value=cfg):
            # 裸记录：id 非时间戳，ts 由 08:00 推导（分钟对齐）
            bare = {
                "entry_id": "bare-20260906-001-aaaa",
                "device_id": "MK8",
                "date": _TODAY,
                "ts": 1788678000000,
                "tag": "",
                "text": "裸记录",
            }
            journal.rebuild_records([bare], [])
            # 之后增量补入一条时间更早的普通时间戳记录 → 应插在裸记录之前
            journal.apply_delta([self._entry("1788677400000", 1788677400000, "早")], [])
        content = (records / f"{_TODAY}.md").read_text(encoding="utf-8")
        self.assertLess(
            content.index("<!-- myrecord-time:1788677400000 -->"),
            content.index("<!-- myrecord-time:bare-20260906-001-aaaa -->"),
        )

    def test_apply_delta_tombstone_keeps_time_position(self):
        """墓碑补齐也按原条目时间插回原位置，而不是堆到文件末尾。"""
        root = _tmp_dir("cli-incr-tomb-")
        records = root / "Records"
        records.mkdir(parents=True, exist_ok=True)
        cfg = {
            "client": {
                "records_dir": records,
                "analysis_dir": root / "A",
                "server_url": "http://x",
            }
        }
        with patch.object(client_config, "load", return_value=cfg):
            journal.rebuild_records(
                [
                    self._entry("426853", 426853, "2"),
                    self._entry("436841", 436841, "我的刀盾"),
                ],
                [],
            )
            # 删除早时间条目（携带原条目时间 entry_ts）→ 墓碑应出现在 426853 位置
            journal.apply_delta(
                [],
                [{"entry_id": "426853", "date": _TODAY, "entry_ts": 426853}],
            )
        content = (records / f"{_TODAY}.md").read_text(encoding="utf-8")
        self.assertIn("myrecord-tombstone-time:426853 -->", content)
        self.assertNotIn("myrecord-time:426853 -->", content)  # 原条目被替换
        self.assertLess(
            content.index("myrecord-tombstone-time:426853 -->"),
            content.index("myrecord-time:436841 -->"),
        )


class ExpiredOutboxTest(ClientSyncE2ETestBase):
    """过期未同步即作废：outbox 里 date < 今天 的条目在 send_pending 时被丢弃，服务端无该条。"""

    def test_expired_outbox_entry_dropped_on_send_pending(self):
        import json

        root = _tmp_dir("exp-outbox-")
        client, pat = self._new_client(self.device_a, self.token_a, root)
        try:
            expired = {
                "entry_id": "expired-1",
                "device_id": self.device_a,
                "date": "2000-01-01",
                "ts": 946684800000,
                "tag": "",
                "text": "昨天没同步的过期条目",
            }
            outbox_path = root / "state" / "outbox.json"
            outbox_path.write_text(json.dumps({"entries": [expired]}), encoding="utf-8")

            client.send_pending()

            # 过期条目被作废：outbox 清空（不再重试）
            self.assertEqual(json.loads(outbox_path.read_text(encoding="utf-8"))["entries"], [])
            # 服务端无该条（双保险）
            self.assertEqual(self.store.data["entries"], {})
        finally:
            self._stop(pat)


class HistoryWholeFileSyncTest(ClientSyncE2ETestBase):
    """历史日整文件校验：本地历史文件与云端不一致 → full_sync 后按云端整文件覆盖；
    本地多余历史文件被删除（云端权威）。"""

    def test_full_sync_overwrites_local_history_and_removes_extra(self):
        root = _tmp_dir("hist-")
        srv_records = self._data / "Records"
        srv_records.mkdir(parents=True, exist_ok=True)
        # 让 /api/records 能读到云端整文件（历史日只读、整文件为准）
        self.store.records_dir = srv_records
        cloud_history = "# 2000-01-01\n\n<summary>\n云端总结\n</summary>\n\n**08:00:** 云端内容\n"
        (srv_records / "2000-01-01.md").write_text(cloud_history, encoding="utf-8")

        client, pat = self._new_client(self.device_a, self.token_a, root)
        try:
            base = root / "Records"
            base.mkdir(parents=True, exist_ok=True)
            # 本地历史文件内容与云端不一致 → 应被云端整文件覆盖
            (base / "2000-01-01.md").write_text("# 2000-01-01\n\n本地旧内容\n", encoding="utf-8")
            # 本地多余历史文件：云端无此日 → 应被删除
            (base / "1999-12-31.md").write_text("# 1999-12-31\n\n本地多余\n", encoding="utf-8")

            client.full_sync()

            # 本地历史文件已按云端整文件覆盖（含旧格式、summary，不解析、不重排）
            self.assertEqual(
                (base / "2000-01-01.md").read_text(encoding="utf-8"), cloud_history
            )
            # 本地多余历史文件被删除（云端权威）
            self.assertFalse((base / "1999-12-31.md").exists())
        finally:
            self._stop(pat)


class OnlyWriteTodayTest(unittest.TestCase):
    """客户端只写“今天”：append_record 对 date < 今天 的条目空操作（不落盘）；今天正常写。"""

    def test_append_record_rejects_past_date_but_writes_today(self):
        root = _tmp_dir("write-today-")
        records = root / "Records"
        records.mkdir(parents=True, exist_ok=True)
        cfg = {
            "client": {
                "records_dir": records,
                "analysis_dir": root / "A",
                "server_url": "http://x",
            }
        }
        with patch.object(client_config, "load", return_value=cfg):
            # 过期条目：不落盘
            journal.append_record({"entry_id": "old-1", "device_id": "d", "date": "2000-01-01", "ts": 1, "tag": "", "text": "过期"})
            self.assertFalse((records / "2000-01-01.md").exists())
            # 今天条目：正常写
            today = journal.today_utc8()
            journal.append_record({"entry_id": "t-1", "device_id": "d", "date": today, "ts": 2, "tag": "", "text": "今天内容"})
            self.assertIn("今天内容", (records / f"{today}.md").read_text(encoding="utf-8"))
            # 本地没有今天的过期残留条目
            self.assertFalse((records / "2000-01-01.md").exists())


if __name__ == "__main__":
    unittest.main()