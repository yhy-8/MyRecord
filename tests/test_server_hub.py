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

    def test_tombstone_preserves_original_entry_ts(self):
        """墓碑记录原条目时间，供渲染时按它把占位符插回记录流原位置。"""
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        store.append_entries("a", [{"entry_id": "a-1", "date": "2024-01-01", "ts": 1704067230, "tag": "", "text": "x"}])
        store.tombstone("a-1", "a")
        tomb = store.data["tombstones"]["a-1"]
        self.assertEqual(1704067230, tomb["entry_ts"])

    def test_append_entries_does_not_resurrect_tombstoned(self):
        """重推已删条目不得复活：append_entries 须跳过墓碑中的 entry_id，且上报 accepted。

        场景：客户端离线队列重推一条已被按删除标记删掉的条目（例如推送响应丢失后重发），
        服务端不能把它重新加入 entries，否则删除会被回滚、在服务端与各端复活。
        """
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        store.append_entries("a", [{"entry_id": "x-1", "date": "2024-01-01", "ts": 5, "tag": "", "text": "将被删"}])
        store.tombstone("x-1", "a")
        # 删除后重推同一 entry_id（模拟客户端 outbox 重发）
        accepted = store.append_entries("a", [{"entry_id": "x-1", "date": "2024-01-01", "ts": 5, "tag": "", "text": "将被删"}])
        self.assertIn("x-1", accepted)  # 视为已接受，客户端可清掉 outbox
        self.assertNotIn("x-1", store.data["entries"])  # 不复活
        self.assertIn("x-1", store.data["tombstones"])
        # 新条目不受影响
        store.append_entries("a", [{"entry_id": "y-2", "date": "2024-01-01", "ts": 6, "tag": "", "text": "新"}])
        self.assertIn("y-2", store.data["entries"])

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

    def test_wait_for_change_returns_tombstone_after_delete(self):
        """长轮询等待路径：A 删后，B 的 wait_for_change 返回墓碑（用于扇出删除）。"""
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        store.append_entries("A", [{"entry_id": "x1", "date": "2024-01-01", "ts": 5, "tag": "", "text": "hi"}])
        store.tombstone("x1", "A")
        delta = store.wait_for_change(after_version=0, timeout=0.05)
        self.assertIn("x1", [t["entry_id"] for t in delta["tombstones"]])
        self.assertEqual([], delta["entries"])  # x1 已删，不在活跃条目中


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

    def test_render_separates_entry_blocks_with_blank_lines(self):
        """渲染出的条目/墓碑块之间必须有空行，否则多端同步后记录会连成一行。"""
        entries = [
            {"entry_id": "a-1", "date": "2024-01-01", "ts": 1704067200, "tag": "", "text": "hello"},
            {"entry_id": "b-1", "date": "2024-01-01", "ts": 1704067260, "tag": "", "text": "world"},
        ]
        tombstones = [{"entry_id": "c-1", "date": "2024-01-01", "v": 3, "ts": 1}]
        text = render.render_day_file("2024-01-01", entries, tombstones)
        self.assertIn("hello\n\n", text)
        self.assertIn("world\n\n", text)

    def test_render_interleaves_tombstone_in_original_position(self):
        """墓碑必须按原条目时间插回记录流原位置，而不是堆到末尾。"""
        entries = [
            {"entry_id": "a-1", "date": "2024-01-01", "ts": 1704067200, "tag": "", "text": "first"},
            {"entry_id": "c-1", "date": "2024-01-01", "ts": 1704067260, "tag": "", "text": "third"},
        ]
        # b-1 原本位于 ts=1704067230（第二条），被删后应占其原位置
        tombstones = [
            {"entry_id": "b-1", "date": "2024-01-01", "v": 4, "ts": 1704068000, "entry_ts": 1704067230},
        ]
        text = render.render_day_file("2024-01-01", entries, tombstones)
        first_pos = text.index("first")
        tomb_pos = text.index("myrecord-tombstone-time:b-1")
        third_pos = text.index("third")
        self.assertTrue(first_pos < tomb_pos < third_pos)

    def test_render_tombstone_without_entry_ts_falls_back_to_deletion_ts(self):
        """墓碑缺少原条目时间：回退到删除时间 ts（通常晚于条目），排在活跃条目之后。"""
        entries = [
            {"entry_id": "a-1", "date": "2024-01-01", "ts": 1704067200, "tag": "", "text": "first"},
        ]
        tombstones = [{"entry_id": "b-1", "date": "2024-01-01", "v": 4, "ts": 1704068000}]
        text = render.render_day_file("2024-01-01", entries, tombstones)
        self.assertLess(text.index("first"), text.index("myrecord-tombstone-time:b-1"))

    def test_render_orders_same_second_by_ms(self):
        """同秒多条记录：毫秒级 ts 决定先后，而非被 entry_id 哈希打乱。"""
        entries = [
            # 同一秒(1704067200)，毫秒不同；entry_id 字典序与 ms 顺序相反
            {"entry_id": "z-2", "date": "2024-01-01", "ts": 1704067200123, "tag": "", "text": "1"},
            {"entry_id": "a-1", "date": "2024-01-01", "ts": 1704067200246, "tag": "", "text": "2"},
        ]
        text = render.render_day_file("2024-01-01", entries)
        # 按 ms 顺序（先 1 后 2），而非按 entry_id 字典序（a-1 本应在前）
        self.assertLess(
            text.index("<!-- myrecord-time:z-2 -->"),
            text.index("<!-- myrecord-time:a-1 -->"),
        )

    def test_fmt_hhmm_from_ms(self):
        """毫秒时间戳换算为正确 HH:MM。"""
        self.assertTrue(render._fmt_hhmm(1704067200246).endswith(":00"))

    def test_fmt_hhmm_uses_utc8(self):
        """展示时间固定按 UTC+8：epoch 1717200000000ms = 2024-01-01 00:00 UTC = 08:00 UTC+8。"""
        self.assertEqual(render._fmt_hhmm(1717200000000), "08:00")
        self.assertEqual(render._fmt_hhmm(1717200000123), "08:00")

    def test_parse_recovers_exact_ts_from_timestamp_id(self):
        """新格式 id=毫秒时间戳：解析文件可还原精确子秒 ts，往返不丢精度。"""
        entries = [
            {"entry_id": "1717200000123", "date": "2024-06-01", "ts": 1717200000123, "tag": "", "text": "a"},
            {"entry_id": "1717200001240", "date": "2024-06-01", "ts": 1717200001240, "tag": "", "text": "b"},
        ]
        text = render.render_day_file("2024-06-01", entries)
        parsed = parse_day_file("2024-06-01", text)
        got = {e["entry_id"]: e["ts"] for e in parsed["entries"]}
        self.assertEqual(got["1717200000123"], 1717200000123)
        self.assertEqual(got["1717200001240"], 1717200001240)

    def test_parse_bare_records(self):
        """无 myrecord-time 标记的裸 `**HH:MM:**` 记录也能解析，并按位置派生确定性 id。"""
        bare = (
            "# 2024-01-01\n\n<summary>\n暂无今日总结。\n</summary>\n\n---\n## 原始记录流\n\n"
            "**08:00:** 旧记录\n\n"
            "**09:00 [引用]:** 引用记录\n"
        )
        parsed = parse_day_file("2024-01-01", bare)
        self.assertEqual(len(parsed["entries"]), 2)
        self.assertTrue(parsed["entries"][0]["entry_id"].startswith("bare-"))
        self.assertEqual(parsed["entries"][1]["tag"], "[引用]")
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

    def test_append_normalizes_path_traversal_date(self):
        """date 含 `../` 会被规范为合法日期，避免渲染写出数据目录之外。"""
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        store.append_entries(
            "a",
            [{"entry_id": "x-1", "date": "../../escape", "ts": 1704067200, "tag": "", "text": "hi"}],
        )
        stored_date = store.data["entries"]["x-1"]["date"]
        self.assertNotIn("..", stored_date)
        datetime.date.fromisoformat(stored_date)  # 必须可解析为合法日期

        records = _tmp_data_dir() / "Records"
        store.render_records(records, records.parent / "Trash")
        self.assertFalse((records.parent / "escape.md").exists())


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