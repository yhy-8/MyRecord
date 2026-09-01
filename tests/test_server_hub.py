"""P1 服务端 hub 测试：鉴权、存储合并、tombstone、渲染、HTTP 同步。"""

import datetime
import tempfile
import threading
import time
import unittest
from pathlib import Path

from server.hub import auth, render
from server.hub.render import parse_day_file
from server.hub.store import Store
from server.hub.server import serve

import requests


def _tmp_data_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="myrecord-p1-"))


class AuthTest(unittest.TestCase):
    def test_hash_roundtrip(self):
        token = auth.new_token()
        stored = auth.hash_token(token)
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertTrue(auth.verify_token(token, stored))
        self.assertFalse(auth.verify_token(token + "x", stored))
        self.assertFalse(auth.verify_token(token, "not-a-hash"))

    def test_slugify(self):
        self.assertEqual(auth.slugify("phone a"), "phone-a")
        self.assertEqual(auth.slugify("phone"), "phone")
        self.assertEqual(auth.slugify("  "), "")


class StoreTest(unittest.TestCase):
    def test_append_dedupe_and_version(self):
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        entries = [
            {"entry_id": "a-1", "date": "2024-01-01", "ts": 1704067200, "tag": "", "text": "hello"},
            {"entry_id": "b-1", "date": "2024-01-01", "ts": 1704067260, "tag": "", "text": "world"},
        ]
        accepted = store.append_entries("a", entries)
        self.assertEqual(accepted, ["a-1", "b-1"])
        self.assertEqual(store.data["version"], 2)
        # 再次推送同一条不重复
        accepted2 = store.append_entries("a", [entries[0]])
        self.assertEqual(accepted2, [])
        self.assertEqual(store.data["version"], 2)

    def test_pull_increment(self):
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        store.append_entries("a", [{"entry_id": "a-1", "date": "2024-01-01", "ts": 1, "tag": "", "text": "x"}])
        delta = store.pull(0)
        self.assertEqual(len(delta["entries"]), 1)
        self.assertEqual(delta["version"], 1)
        empty = store.pull(1)
        self.assertEqual(empty["entries"], [])
        self.assertEqual(empty["tombstones"], [])

    def test_tombstone_moves_to_trash(self):
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        store.append_entries("a", [{"entry_id": "a-1", "date": "2024-01-01", "ts": 5, "tag": "", "text": "secret"}])
        ok = store.tombstone("a-1", "a")
        self.assertTrue(ok)
        self.assertNotIn("a-1", store.data["entries"])
        self.assertIn("a-1", store.data["trash"])
        self.assertEqual(store.data["trash"]["a-1"]["text"], "secret")
        self.assertIn("a-1", store.data["tombstones"])

    def test_latest_entry_for_date(self):
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        store.append_entries("a", [
            {"entry_id": "a-1", "date": "2024-01-01", "ts": 5, "tag": "", "text": "first"},
            {"entry_id": "b-1", "date": "2024-01-01", "ts": 8, "tag": "", "text": "second"},
        ])
        latest = store.latest_entry_for_date("2024-01-01")
        self.assertEqual(latest["entry_id"], "b-1")
        self.assertIsNone(store.latest_entry_for_date("2024-01-02"))


class RenderParserTest(unittest.TestCase):
    def test_render_and_parse_roundtrip(self):
        entries = [
            {"entry_id": "a-1", "date": "2024-01-01", "ts": 1704067200, "tag": "", "text": "hello"},
            {"entry_id": "b-1", "date": "2024-01-01", "ts": 1704067260, "tag": "[引用]", "text": "world"},
        ]
        tombstones = [{"entry_id": "c-1", "date": "2024-01-01", "v": 3, "ts": 1}]
        text = render.render_day_file("2024-01-01", entries, tombstones, summary="今日总结")
        self.assertIn("<summary>\n今日总结\n</summary>", text)
        parsed = parse_day_file("2024-01-01", text)
        ids = [e["entry_id"] for e in parsed["entries"]]
        self.assertEqual(ids, ["a-1", "b-1"])
        self.assertEqual(parsed["entries"][1]["tag"], "[引用]")
        self.assertEqual(parsed["tombstones"], ["c-1"])

    def test_parse_legacy(self):
        legacy = (
            "# 2024-01-01\n\n<summary>\n暂无今日总结。\n</summary>\n\n---\n## 原始记录流\n\n"
            "<!-- agentrecord-record -->\n**08:00:** 旧记录\n\n"
            "<!-- agentrecord-record-text -->\n**09:00 [引用]:** 引用记录\n"
        )
        parsed = parse_day_file("2024-01-01", legacy)
        self.assertEqual(len(parsed["entries"]), 2)
        self.assertTrue(parsed["entries"][0]["entry_id"].startswith("legacy-"))
        # 旧标记是注释，不允许混入正文；AI 只应读到纯文本。
        for entry in parsed["entries"]:
            self.assertNotIn("<!--", entry["text"])
            self.assertNotIn("agentrecord", entry["text"])


class StoreDeviceTests(unittest.TestCase):
    """单一链接凭证：签发覆盖旧凭证、校验/轮换/撤销、快照与损坏恢复。"""

    def test_register_replaces_existing_single_token(self):
        """签发新 token 直接覆盖并删除旧 token（单一凭证模型）。"""
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        token1 = auth.new_token()
        token2 = auth.new_token()
        store.register_device("phone", token1)
        self.assertTrue(store.verify_device("any-name", token1))  # 凭证不绑定设备名
        # 重新签发 → 旧 token 失效，只剩新 token
        store.register_device("phone", token2)
        self.assertFalse(store.verify_device("any-name", token1))
        self.assertTrue(store.verify_device("any-name", token2))

    def test_device_ids_only_include_active(self):
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        store.register_device("b", auth.new_token())
        store.register_device("a", auth.new_token())  # 单一凭证：覆盖旧 token
        ids = store.device_ids()
        self.assertEqual(["a"], ids)  # 只剩当前唯一凭证

    def test_active_credential_includes_created_at(self):
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        # 未签发：无有效凭证
        self.assertIsNone(store.active_credential())

        token = auth.new_token()
        device = store.register_device("dev", token)
        cred = store.active_credential()
        self.assertEqual("dev", cred["device_id"])
        self.assertGreater(cred["created_at"], 0)

        # 重签覆盖旧信息：device_id 与 created_at 随之更新
        store.register_device("new", token)
        self.assertEqual("new", store.active_credential()["device_id"])

    def test_snapshot_excludes_token_hashes(self):
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        store.register_device("dev", auth.new_token())
        snap = store.snapshot()
        self.assertIn("active", snap["devices"]["dev"])
        self.assertNotIn("token_hash", snap["devices"]["dev"])

    def test_load_corrupt_state_recovers_defaults(self):
        data = _tmp_data_dir()
        state_file = data / "state.json"
        state_file.write_text("{not valid json", encoding="utf-8")
        store = Store(state_file)
        self.assertEqual(0, store.data["version"])
        self.assertEqual({}, store.data["entries"])
        self.assertEqual({}, store.data["devices"])


class StoreRenderTest(unittest.TestCase):
    """store.render_records：Records 桶渲染、垃圾桶渲染与已有 summary 保留。"""

    def _store_with_entries(self, state_file):
        store = Store(state_file)
        store.append_entries(
            "a",
            [{"entry_id": "a-1", "date": "2024-01-01", "ts": 1, "tag": "", "text": "保留正文"}],
        )
        store.append_entries(
            "b",
            [{"entry_id": "b-1", "date": "2024-01-01", "ts": 5, "tag": "", "text": "待删除正文"}],
        )
        store.tombstone("b-1", "b")
        return store

    def test_render_records_writes_entries_and_trash(self):
        data = _tmp_data_dir() / "state.json"
        store = self._store_with_entries(data)
        records_dir = data.parent / "Records"
        trash_dir = data.parent / "Trash"

        store.render_records(records_dir, trash_dir)

        records = (records_dir / "2024-01-01.md").read_text(encoding="utf-8")
        self.assertIn("保留正文", records)
        self.assertNotIn("待删除正文", records)  # 已删正文应收进垃圾桶
        self.assertIn("myrecord-tombstone", records)

        trash = (trash_dir / "2024-01-01.md").read_text(encoding="utf-8")
        self.assertIn("待删除正文", trash)
        self.assertNotIn("保留正文", trash)

    def test_render_preserves_existing_summary(self):
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        store.append_entries(
            "a",
            [{"entry_id": "a-1", "date": "2024-01-01", "ts": 1, "tag": "", "text": "新记录"}],
        )
        records_dir = data.parent / "Records"
        records_dir.mkdir(parents=True, exist_ok=True)
        (records_dir / "2024-01-01.md").write_text(
            "# 2024-01-01\n\n<summary>\n已有总结\n</summary>\n\n---\n## 原始记录流\n",
            encoding="utf-8",
        )

        store.render_records(records_dir, data.parent / "Trash")

        content = (records_dir / "2024-01-01.md").read_text(encoding="utf-8")
        self.assertIn("已有总结", content)
        self.assertIn("新记录", content)

    def test_tombstone_is_idempotent_and_rejects_missing(self):
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        store.append_entries(
            "a",
            [{"entry_id": "a-1", "date": "2024-01-01", "ts": 1, "tag": "", "text": "x"}],
        )
        self.assertTrue(store.tombstone("a-1", "a"))
        self.assertFalse(store.tombstone("a-1", "a"))
        self.assertFalse(store.tombstone("does-not-exist", "a"))

    def test_append_derives_date_from_timestamp_when_missing(self):
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        store.append_entries(
            "a",
            [{"entry_id": "a-1", "ts": 0, "text": "x"}],
        )
        entry = store.data["entries"]["a-1"]
        self.assertEqual(datetime.date.today().isoformat(), entry["date"])

    def test_append_rejects_missing_entry_id(self):
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        accepted = store.append_entries("a", [{"text": "no-id"}])
        self.assertEqual([], accepted)
        self.assertEqual(0, store.data["version"])


class AdminEndpointTest(unittest.TestCase):
    def setUp(self):
        self._dir = _tmp_data_dir()
        self.store = Store(self._dir / "state.json")
        token = auth.new_token()
        self.device_id = self.store.register_device("admin-test", token)
        self.token = token
        self.httpd = serve(
            self.store,
            "127.0.0.1",
            0,
            admin_retry=lambda: (True, "全部重试成功"),
            admin_set_model=lambda name: (True, f"已切换为 {name}"),
            status_ai=lambda: {
                "current_model": "deepseek-v4-flash",
                "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
            },
        )
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.port}"
        self.headers = {"Authorization": f"Bearer {token}", "X-Device-Id": self.device_id}

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_status_includes_ai(self):
        resp = requests.get(f"{self.base}/api/status", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        ai = resp.json().get("ai") or {}
        self.assertEqual(ai.get("current_model"), "deepseek-v4-flash")
        self.assertEqual(len(ai.get("models")), 2)

    def test_admin_retry(self):
        resp = requests.post(f"{self.base}/api/admin/retry", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["message"], "全部重试成功")

    def test_admin_model(self):
        resp = requests.post(
            f"{self.base}/api/admin/model",
            headers=self.headers,
            json={"name": "deepseek-v4-pro"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["message"], "已切换为 deepseek-v4-pro")

    def test_reports_list_and_download(self):
        # list_reports / read_report 指向内存示例报告
        samples = {"Weekly/2024-01-01_to_2024-01-07_auto.md": "# 周报\n内容\n"}
        self.httpd.list_reports = lambda kind: [
            "Weekly/2024-01-01_to_2024-01-07_auto.md"
        ]
        self.httpd.read_report = lambda rel: samples.get(rel)
        listed = requests.get(
            f"{self.base}/api/reports?kind=Weekly", headers=self.headers
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()["reports"]), 1)
        content = requests.get(
            f"{self.base}/api/reports/Weekly/2024-01-01_to_2024-01-07_auto.md",
            headers=self.headers,
        )
        self.assertEqual(content.status_code, 200)
        self.assertIn("周报", content.text)


class ServerHttpTest(unittest.TestCase):
    def setUp(self):
        self._dir = _tmp_data_dir()
        self.store = Store(self._dir / "state.json")
        token = auth.new_token()
        self.device_id = self.store.register_device("client-A", token)
        self.token = token
        self.httpd = serve(self.store, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "X-Device-Id": self.device_id,
        }

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _push_one(self, entry, version=0):
        return requests.post(
            f"{self.base}/api/sync/push",
            headers=self.headers,
            json={"entries": [entry], "version": version},
        )

    def test_push_and_pull(self):
        resp = self._push_one({"entry_id": "a-1", "date": "2024-01-01", "ts": 5, "tag": "", "text": "x"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["accepted"], ["a-1"])
        self.assertEqual(body["version"], 1)
        pull = requests.get(f"{self.base}/api/sync/pull?version=0", headers=self.headers)
        self.assertEqual(pull.status_code, 200)
        self.assertEqual(len(pull.json()["entries"]), 1)

    def test_unauthorized(self):
        bad = {"Authorization": "Bearer wrong", "X-Device-Id": self.device_id}
        resp = requests.get(f"{self.base}/api/sync/pull?version=0", headers=bad)
        self.assertEqual(resp.status_code, 401)

    def test_delete(self):
        self._push_one({"entry_id": "a-1", "date": "2024-01-01", "ts": 5, "tag": "", "text": "x"})
        self._push_one({"entry_id": "b-1", "date": "2024-01-01", "ts": 8, "tag": "", "text": "y"})
        resp = requests.post(
            f"{self.base}/api/entries/delete",
            headers=self.headers,
            json={"date": "2024-01-01"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["deleted"], "b-1")
        pull = requests.get(f"{self.base}/api/sync/pull?version=0", headers=self.headers)
        self.assertEqual(pull.json()["tombstones"][0]["entry_id"], "b-1")

    def test_longpoll_fanout(self):
        # 长轮询线程挂起后，推送新条目应触发扇出返回
        result = {}

        def poll():
            result["resp"] = requests.get(
                f"{self.base}/api/sync/longpoll?version=0",
                headers=self.headers,
                timeout=30,
            )

        t = threading.Thread(target=poll)
        t.start()
        time.sleep(0.3)
        self._push_one({"entry_id": "a-1", "date": "2024-01-01", "ts": 5, "tag": "", "text": "x"})
        t.join(timeout=10)
        self.assertIn("resp", result)
        self.assertEqual(result["resp"].status_code, 200)
        self.assertEqual(len(result["resp"].json()["entries"]), 1)

    def test_status(self):
        self._push_one({"entry_id": "a-1", "date": "2024-01-01", "ts": 5, "tag": "", "text": "x"})
        resp = requests.get(f"{self.base}/api/status", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["entry_count"], 1)
        self.assertIn(self.device_id, body["devices"])

    def test_failed_token_lockout(self):
        bad = {"Authorization": "Bearer nope", "X-Device-Id": self.device_id}
        # 连续失败 5 次（阈值）后进入锁定
        for _ in range(5):
            requests.get(f"{self.base}/api/sync/pull?version=0", headers=bad)
        # 锁定期内即使令牌正确也拒绝
        locked = requests.get(f"{self.base}/api/sync/pull?version=0", headers=self.headers)
        self.assertEqual(locked.status_code, 401)


if __name__ == "__main__":
    unittest.main()