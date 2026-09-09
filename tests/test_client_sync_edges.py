"""client/sync.py 的异常与边界路径测试（在线删除、鉴权、网络、422 拒绝、报告清理）。

这些是真实使用中会触发的路径：断网、鉴权失败、服务端拒绝批次、历史日网络错误、
本地报告清理、删除非今天。用 mock 隔离网络，避免依赖真实服务端可用性。
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from client import config as client_config
from client import identity as client_identity
from client import journal
from client import sync
from client.sync import SyncClient, SyncError

_TODAY = journal.today_utc8()
_YESTERDAY = "2000-01-01"


def _tmp(prefix):
    return Path(tempfile.mkdtemp(prefix=prefix))


def _cfg(records_dir, analysis_dir):
    return {"client": {"server_url": "http://127.0.0.1:1",
                       "records_dir": records_dir,
                       "analysis_dir": analysis_dir,
                       "verify": ""}}


class _Resp:
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code = status_code
        self._data = data
        self.text = text

    def json(self):
        return self._data


class SyncClientEdgeBase(unittest.TestCase):
    def setUp(self):
        self._root = _tmp("myrecord-client-")
        records = self._root / "Records"
        analysis = self._root / "AnalysisReports"
        state = self._root / "state"
        records.mkdir(parents=True, exist_ok=True)
        analysis.mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True)
        self.config = _cfg(records, analysis)
        self.patches = [
            patch.object(sync, "_state_path", return_value=state / "state.json"),
            patch.object(sync, "_outbox_path", return_value=state / "outbox.json"),
            patch.object(client_config, "load", return_value=self.config),
            patch.object(client_identity, "load", return_value={"token": "tok"}),
            patch.object(client_identity, "device_name", return_value="edge-host"),
        ]
        for p in self.patches:
            p.start()
        self.client = SyncClient(server_url="http://127.0.0.1:1")
        self.records = records

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def _write_local(self, date, content):
        path = self.records / f"{date}.md"
        path.write_text(content, encoding="utf-8")
        return path


class RequestErrorTests(SyncClientEdgeBase):
    @patch("client.sync.requests.Session.request", side_effect=sync.requests.ConnectionError("refused"))
    def test_request_connection_error_raises_sync_error(self, _req):
        with self.assertRaises(SyncError) as ctx:
            self.client._request("GET", "/api/status")
        self.assertIn("无法连接服务端", str(ctx.exception))

    @patch("client.sync.requests.Session.request", return_value=_Resp(401))
    def test_request_401_raises_auth_failure(self, _req):
        with self.assertRaises(SyncError) as ctx:
            self.client._request("GET", "/api/status")
        self.assertIn("鉴权失败", str(ctx.exception))

    @patch("client.sync.requests.Session.request", return_value=_Resp(500))
    def test_request_500_raises_server_error(self, _req):
        with self.assertRaises(SyncError) as ctx:
            self.client._request("GET", "/api/status")
        self.assertIn("500", str(ctx.exception))

    @patch("client.sync.requests.Session.request", side_effect=sync.requests.ConnectTimeout("t"))
    def test_request_payload_network_error_raises(self, _req):
        with self.assertRaises(SyncError):
            self.client._request_payload("POST", "/api/sync/push", json_body={})

    @patch("client.sync.requests.Session.request", return_value=_Resp(401))
    def test_request_payload_401_raises_auth_failure(self, _req):
        with self.assertRaises(SyncError):
            self.client._request_payload("POST", "/api/sync/push", json_body={})

    @patch("client.sync.requests.Session.request", return_value=_Resp(200, None))
    def test_request_payload_non_json_returns_none(self, _req):
        status, payload = self.client._request_payload("GET", "/api/status")
        self.assertEqual(200, status)
        self.assertIsNone(payload)


class HeadersValidationTests(SyncClientEdgeBase):
    def test_non_ascii_token_raises_sync_error_not_crash(self):
        # 复制样板时若 token 仍是中文占位/误粘中文，requests 构造头会抛 UnicodeEncodeError。
        # 这里应提前转成可处理的 SyncError，而不是崩栈。
        with patch.object(client_identity, "load", return_value={"token": "中文占位"}):
            with self.assertRaises(SyncError) as ctx:
                self.client._headers()
        self.assertIn("非 ASCII", str(ctx.exception))

    def test_ascii_token_ok(self):
        self.assertEqual("Bearer tok", self.client._headers()["Authorization"])


class ProbeTests(SyncClientEdgeBase):
    @patch("client.sync.requests.Session.get", return_value=_Resp(503))
    def test_probe_non_200_reports_error(self, _get):
        result = self.client.probe()
        self.assertFalse(result["connected"])
        self.assertIn("503", result["error"])

    @patch("client.sync.requests.Session.get", side_effect=sync.requests.ConnectionError("down"))
    def test_probe_connection_error_sets_compact_message(self, _get):
        result = self.client.probe()
        self.assertFalse(result["connected"])
        self.assertIn("连接被拒绝或网络不可达", result["error"])


class SendPendingTests(SyncClientEdgeBase):
    def _outbox(self, entries):
        sync._save_outbox(entries)

    @patch("client.sync.requests.Session.request")
    def test_send_pending_422_drops_rejected_keeps_others(self, req):
        # 服务端拒绝批次：rejected 里的 id 作废移除，其余保留
        self._outbox([
            {"entry_id": "a", "date": _TODAY, "text": "x"},
            {"entry_id": "b", "date": _TODAY, "text": "y"},
        ])
        req.return_value = _Resp(
            422, {"ok": False, "error": "expired", "rejected": ["a"]}
        )
        result = self.client.send_pending()
        self.assertEqual(["a"], result.get("rejected"))
        remaining = sync._load_outbox()
        self.assertEqual(["b"], [e["entry_id"] for e in remaining])
        # 未调用 _apply_delta（422 不走正常对账）
        self.assertEqual(0, sync._read_state())

    @patch("client.sync.requests.Session.request")
    def test_send_pending_404_raises_server_error_and_keeps_outbox(self, req):
        self._outbox([{"entry_id": "a", "date": _TODAY, "text": "x"}])
        req.return_value = _Resp(404)
        with self.assertRaises(SyncError):
            self.client.send_pending()
        # outbox 保留，等待下次重试
        self.assertEqual(1, len(sync._load_outbox()))

    @patch("client.sync.requests.Session.request")
    def test_send_pending_200_removes_accepted(self, req):
        self._outbox([{"entry_id": "a", "date": _TODAY, "text": "x"}])
        req.return_value = _Resp(200, {
            "accepted": ["a"], "version": 1, "entries": [], "tombstones": [],
        })
        self.client.send_pending()
        self.assertEqual([], sync._load_outbox())
        self.assertEqual(1, sync._read_state())


class DeleteLatestTests(SyncClientEdgeBase):
    def test_delete_latest_skips_non_today(self):
        self.assertIsNone(self.client.delete_latest(_YESTERDAY))

    @patch("client.sync.requests.Session.request", return_value=_Resp(200, {"deleted": "x"}))
    def test_delete_latest_forwards_version(self, req):
        self.client.delete_latest(_TODAY)
        body = req.call_args.kwargs.get("json")
        self.assertEqual(_TODAY, body["date"])
        self.assertEqual(0, body["version"])


class HistorySyncErrorTests(SyncClientEdgeBase):
    @patch("client.sync.requests.Session.request", side_effect=sync.requests.ConnectionError("down"))
    def test_sync_history_files_survives_network_error(self, req):
        # 服务端不可达：保留本地历史文件，不抛异常、不误删
        path = self._write_local(_YESTERDAY, "# old\n内容")
        self.client.sync_history_files()
        # 服务端不可达：保留本地历史文件，不误删、不抛异常
        self.assertTrue(path.exists())

    @patch("client.sync.requests.Session.request")
    def test_sync_history_files_removes_local_extra_when_online(self, req):
        # 云端清单不含历史日 → 本地多余历史文件被删除（云端为准）
        path = self._write_local(_YESTERDAY, "# old 本地多余\n内容")
        req.return_value = _Resp(200, {"files": [{"date": _TODAY, "sha256": "faker"}]})
        self.client.sync_history_files()
        self.assertFalse(path.exists())

    @patch("client.sync.requests.Session.request")
    def test_sync_history_files_keeps_local_when_cloud_sha_empty(self, req):
        # 云端列出该日期但 sha256 为空（服务端文件不可读）→ 不得删本地副本
        content = "# old\n内容"
        path = self._write_local(_YESTERDAY, content)

        def respond(method, url, **kwargs):
            if url.endswith("/api/records"):
                return _Resp(200, {"files": [{"date": _YESTERDAY, "sha256": ""}]})
            return _Resp(500, {"error": "read failed"})

        req.side_effect = respond
        self.client.sync_history_files()
        self.assertTrue(path.exists(), "清单列出但哈希未知时不得删除本地副本")
        self.assertEqual(content, path.read_text(encoding="utf-8"))


class ReportSyncCleanupTests(SyncClientEdgeBase):
    @patch("client.sync.requests.Session.request")
    def test_sync_reports_removes_stale_local_report(self, req):
        base = self.config["client"]["analysis_dir"]
        stale = base / "Monthly" / "2026-07.md"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("# 旧报告", encoding="utf-8")
        req.return_value = _Resp(
            200, {"files": [{"rel": "Weekly/2026-07-06_to_2026-07-12.md",
                             "sha256": "faker"}]}
        )
        self.client.sync_reports()
        self.assertFalse(stale.exists(), "云端已不存在的本地报告应被清理")


if __name__ == "__main__":
    unittest.main()
