"""P1 服务端 hub 测试：鉴权、存储合并、tombstone、渲染、HTTP 同步。

方案 B 语义：**唯一可写窗口 = “今天”（UTC+8 自然日）**。历史日只读、以整文件为准；
客户端只能推送 date == 今天的条目，服务端拒绝历史日（422）。
"""

import datetime
import tempfile
import threading
import time
import unittest
from pathlib import Path

from server.hub import auth, render
from server.hub.store import Store, today_utc8
from server.hub.server import serve

import requests


def _tmp_data_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="myrecord-p1-"))


def _today() -> str:
    return today_utc8()


def _today_ts(hour=12, minute=0, second=0) -> int:
    """返回一个其 UTC+8 派生日期 == 今天的毫秒时间戳（用于构造“今天”条目）。"""
    dt = datetime.datetime.fromisoformat(f"{today_utc8()}T{hour:02d}:{minute:02d}:{second:02d}+08:00")
    return int(dt.timestamp() * 1000)


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
            {"entry_id": "a-1", "date": _today(), "ts": _today_ts(8), "tag": "", "text": "hello"},
            {"entry_id": "b-1", "date": _today(), "ts": _today_ts(9), "tag": "", "text": "world"},
        ]
        accepted, rejected = store.append_entries("a", entries)
        self.assertEqual(accepted, ["a-1", "b-1"])
        self.assertEqual(rejected, [])
        self.assertEqual(store.data["version"], 2)
        # 再次推送同一条不重复，但已被权威处理（已存在）→ 上报 accepted 供客户端清 outbox（不再无限重试）。
        accepted2, _ = store.append_entries("a", [entries[0]])
        self.assertEqual(accepted2, ["a-1"])
        self.assertEqual(store.data["version"], 2)

    def test_append_rejects_history_date(self):
        """date < 今天 的条目被拒绝（不入库、不渲染），返回 rejected。"""
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        accepted, rejected = store.append_entries(
            "a",
            [{"entry_id": "old-1", "date": "2000-01-01", "ts": 946684800000, "tag": "", "text": "历史"}],
        )
        self.assertEqual(accepted, [])
        self.assertEqual(rejected, ["old-1"])
        self.assertEqual(store.data["version"], 0)
        self.assertEqual(store.data["entries"], {})

    def test_append_rejects_whole_batch_when_any_expired(self):
        """批次含过期条目时整体拒绝：有效条目也不入库（客户端作废后单独重推）。"""
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        accepted, rejected = store.append_entries(
            "a",
            [
                {"entry_id": "today-1", "date": _today(), "ts": _today_ts(8), "tag": "", "text": "ok"},
                {"entry_id": "old-1", "date": "2000-01-01", "ts": 946684800000, "tag": "", "text": "历史"},
            ],
        )
        self.assertEqual(accepted, [])
        self.assertEqual(rejected, ["old-1"])
        self.assertEqual(store.data["entries"], {})

    def test_pull_returns_full_today_regardless_of_token(self):
        """游标已降级：pull 始终返回今天全量，客户端按 entry_id 幂等合并。"""
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        store.append_entries(
            "a", [{"entry_id": "a-1", "date": _today(), "ts": _today_ts(8), "tag": "", "text": "x"}]
        )
        # 直接写入 state 的一笔“过去日”残留，验证 today_state 过滤掉它
        store.data["entries"]["old-1"] = {
            "entry_id": "old-1", "device_id": "a", "date": "2000-01-01", "ts": 1, "tag": "", "text": "old",
        }
        first = store.pull(0)
        self.assertEqual([e["entry_id"] for e in first["entries"]], ["a-1"])
        # 传一个更大的令牌仍返回今天全量（不依赖游标筛选，不会漏数据）。
        again = store.pull(99)
        self.assertEqual([e["entry_id"] for e in again["entries"]], ["a-1"])

    def test_wait_for_change_timeout_returns_empty_delta(self):
        """长轮询超时无变化：返回空增量（不重复下发全量），令牌用于唤醒。"""
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        store.append_entries(
            "a", [{"entry_id": "a-1", "date": _today(), "ts": _today_ts(8), "tag": "", "text": "x"}]
        )
        version = store.data["version"]
        # 令牌已是当前值：无变化，超时返回空
        delta = store.wait_for_change(version, timeout=0.05)
        self.assertEqual(delta["entries"], [])
        self.assertEqual(delta["tombstones"], [])
        # 令牌落后：立即返回今天全量
        delta = store.wait_for_change(0, timeout=0.05)
        self.assertEqual([e["entry_id"] for e in delta["entries"]], ["a-1"])

    def test_tombstone_moves_to_trash(self):
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        store.append_entries("a", [{"entry_id": "a-1", "date": _today(), "ts": _today_ts(8), "tag": "", "text": "secret"}])
        ok = store.tombstone("a-1", "a")
        self.assertTrue(ok)
        self.assertNotIn("a-1", store.data["entries"])
        self.assertIn("a-1", store.data["trash"])
        self.assertEqual(store.data["trash"]["a-1"]["text"], "secret")
        self.assertIn("a-1", store.data["tombstones"])

    def test_tombstone_rejects_history_date(self):
        """历史日条目不可删（今天之外只读）；历史日条目态会被封存清理。"""
        import json

        data = _tmp_data_dir() / "state.json"
        # 直接写一个含“历史日条目态”的 state，模拟进程重启后今日窗口未定
        data.write_text(
            json.dumps({
                "version": 1,
                "entries": {"old-1": {"entry_id": "old-1", "device_id": "a", "date": "2000-01-01", "ts": 1, "tag": "", "text": "old", "v": 1}},
                "tombstones": {},
                "trash": {},
                "devices": {},
            }),
            encoding="utf-8",
        )
        # 传入 records_dir，让封存先渲染历史日文件再清理（否则无落盘目录时
        # 封存会跳过清理，以“仅今天常驻 state”语义不再把历史日条目态清掉——那是数据丢失）。
        store = Store(data, data.parent / "Records")
        # 历史日条目不可删（返回 False）
        self.assertFalse(store.tombstone("old-1", "a"))
        # 首次访问触发封存：历史日条目态被移除（不再常驻 state，以整文件为准）
        self.assertNotIn("old-1", store.data["entries"])

    def test_tombstone_preserves_original_entry_ts(self):
        """墓碑记录原条目时间，供渲染时按它把占位符插回记录流原位置。"""
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        ts = _today_ts(9)
        store.append_entries("a", [{"entry_id": "a-1", "date": _today(), "ts": ts, "tag": "", "text": "x"}])
        store.tombstone("a-1", "a")
        tomb = store.data["tombstones"]["a-1"]
        self.assertEqual(ts, tomb["entry_ts"])

    def test_append_entries_does_not_resurrect_tombstoned(self):
        """重推已删条目不得复活：append_entries 须跳过墓碑中的 entry_id，且上报 accepted。

        场景：客户端离线队列重推一条已被按删除标记删掉的条目（例如推送响应丢失后重发），
        服务端不能把它重新加入 entries，否则删除会被回滚、在服务端与各端复活。
        """
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        store.append_entries("a", [{"entry_id": "x-1", "date": _today(), "ts": _today_ts(8), "tag": "", "text": "将被删"}])
        store.tombstone("x-1", "a")
        # 删除后重推同一 entry_id（模拟客户端 outbox 重发）
        accepted, _ = store.append_entries("a", [{"entry_id": "x-1", "date": _today(), "ts": _today_ts(8), "tag": "", "text": "将被删"}])
        self.assertIn("x-1", accepted)  # 视为已接受，客户端可清掉 outbox
        self.assertNotIn("x-1", store.data["entries"])  # 不复活
        self.assertIn("x-1", store.data["tombstones"])
        # 新条目不受影响
        store.append_entries("a", [{"entry_id": "y-2", "date": _today(), "ts": _today_ts(9), "tag": "", "text": "新"}])
        self.assertIn("y-2", store.data["entries"])

    def test_wait_for_change_returns_tombstone_after_delete(self):
        """长轮询等待路径：A 删后，B 的 wait_for_change 返回墓碑（用于扇出删除）。"""
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        store.append_entries("A", [{"entry_id": "x1", "date": _today(), "ts": _today_ts(8), "tag": "", "text": "hi"}])
        store.tombstone("x1", "A")
        delta = store.wait_for_change(after_version=0, timeout=0.05)
        self.assertIn("x1", [t["entry_id"] for t in delta["tombstones"]])
        self.assertEqual([], delta["entries"])  # x1 已删，不在活跃条目中


class RenderTest(unittest.TestCase):
    def test_render_blocks_and_summary(self):
        entries = [
            {"entry_id": "a-1", "date": "2024-01-01", "ts": 1704067200, "tag": "", "text": "hello"},
            {"entry_id": "b-1", "date": "2024-01-01", "ts": 1704067260, "tag": "[引用]", "text": "world"},
        ]
        tombstones = [{"entry_id": "c-1", "date": "2024-01-01", "v": 3, "ts": 1}]
        text = render.render_day_file("2024-01-01", entries, tombstones, summary="今日总结")
        self.assertIn("<summary>\n今日总结\n</summary>", text)
        self.assertIn("hello", text)
        self.assertIn("myrecord-tombstone-time:c-1", text)

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
        tombstones = [
            {"entry_id": "b-1", "date": "2024-01-01", "v": 4, "ts": 1704068000, "entry_ts": 1704067230},
        ]
        text = render.render_day_file("2024-01-01", entries, tombstones)
        self.assertLess(text.index("first"), text.index("myrecord-tombstone-time:b-1"))
        self.assertLess(text.index("myrecord-tombstone-time:b-1"), text.index("third"))

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
            {"entry_id": "z-2", "date": "2024-01-01", "ts": 1704067200123, "tag": "", "text": "1"},
            {"entry_id": "a-1", "date": "2024-01-01", "ts": 1704067200246, "tag": "", "text": "2"},
        ]
        text = render.render_day_file("2024-01-01", entries)
        self.assertLess(
            text.index("<!-- myrecord-time:z-2 -->"),
            text.index("<!-- myrecord-time:a-1 -->"),
        )

    def test_fmt_hhmm_uses_utc8(self):
        self.assertEqual(render._fmt_hhmm(1717200000000), "08:00")
        self.assertEqual(render._fmt_hhmm(1717200000123), "08:00")

    def test_record_pattern_kept_for_ai_context(self):
        """RECORD_PATTERN 仍存在，供 AI 分析链路按天读取记录正文（历史日整文件）。"""
        self.assertIsNotNone(render.RECORD_PATTERN)
        self.assertIsNotNone(render.RECORD_MARKER)
        self.assertIsNotNone(render.ESCAPED_RECORD_MARKER)


class StoreDeviceTests(unittest.TestCase):
    """单一链接凭证：签发覆盖旧凭证、校验/轮换/撤销、快照与损坏恢复。"""

    def test_register_replaces_existing_single_token(self):
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        token1 = auth.new_token()
        token2 = auth.new_token()
        store.register_device("phone", token1)
        self.assertTrue(store.verify_device("any-name", token1))
        store.register_device("phone", token2)
        self.assertFalse(store.verify_device("any-name", token1))
        self.assertTrue(store.verify_device("any-name", token2))

    def test_device_ids_only_include_active(self):
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        store.register_device("b", auth.new_token())
        store.register_device("a", auth.new_token())
        self.assertEqual(["a"], store.device_ids())

    def test_active_credential_includes_created_at(self):
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        self.assertIsNone(store.active_credential())
        token = auth.new_token()
        store.register_device("dev", token)
        cred = store.active_credential()
        self.assertEqual("dev", cred["device_id"])
        self.assertGreater(cred["created_at"], 0)
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
    """store.render_records：只渲染“今天”，保留已有 summary，垃圾桶渲染。"""

    def _store_with_entries(self, state_file):
        store = Store(state_file)
        store.append_entries(
            "a",
            [{"entry_id": "a-1", "date": _today(), "ts": _today_ts(8), "tag": "", "text": "保留正文"}],
        )
        store.append_entries(
            "b",
            [{"entry_id": "b-1", "date": _today(), "ts": _today_ts(9), "tag": "", "text": "待删除正文"}],
        )
        store.tombstone("b-1", "b")
        return store

    def test_render_records_writes_entries_and_trash(self):
        data = _tmp_data_dir() / "state.json"
        store = self._store_with_entries(data)
        records_dir = data.parent / "Records"

        store.render_records(records_dir)

        records = (records_dir / f"{_today()}.md").read_text(encoding="utf-8")
        self.assertIn("保留正文", records)
        self.assertNotIn("待删除正文", records)
        self.assertIn("myrecord-tombstone", records)

        # 单文件垃圾桶 Records/trash.md（不再按天分文件）
        trash = (records_dir / "trash.md").read_text(encoding="utf-8")
        self.assertIn("待删除正文", trash)
        self.assertNotIn("保留正文", trash)

    def test_render_records_empty_day_creates_no_placeholder_file(self):
        """空日（无条目、无墓碑）不生成 Records/<date>.md 占位文件；trash.md 为空。"""
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        records_dir = data.parent / "Records"

        store.render_records(records_dir)

        self.assertFalse((records_dir / f"{_today()}.md").exists())
        self.assertEqual("", (records_dir / "trash.md").read_text(encoding="utf-8"))

    def test_render_records_does_not_touch_history_files(self):
        """render_records 只渲染今天：历史/旧格式整文件不被重排/重写。"""
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        store.append_entries("a", [{"entry_id": "a-1", "date": _today(), "ts": _today_ts(8), "tag": "", "text": "今天"}])
        records_dir = data.parent / "Records"
        records_dir.mkdir(parents=True, exist_ok=True)
        legacy = records_dir / "2000-01-01.md"
        legacy.write_text("# 2000-01-01\n\n旧格式 **08:00:** 内容\n", encoding="utf-8")

        store.render_records(records_dir)

        self.assertEqual(legacy.read_text(encoding="utf-8"), "# 2000-01-01\n\n旧格式 **08:00:** 内容\n")

    def test_render_preserves_existing_summary(self):
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        store.append_entries("a", [{"entry_id": "a-1", "date": _today(), "ts": _today_ts(8), "tag": "", "text": "新记录"}])
        records_dir = data.parent / "Records"
        records_dir.mkdir(parents=True, exist_ok=True)
        (records_dir / f"{_today()}.md").write_text(
            f"# {_today()}\n\n<summary>\n已有总结\n</summary>\n\n---\n## 原始记录流\n",
            encoding="utf-8",
        )

        store.render_records(records_dir)

        content = (records_dir / f"{_today()}.md").read_text(encoding="utf-8")
        self.assertIn("已有总结", content)
        self.assertIn("新记录", content)

    def test_tombstone_is_idempotent_and_rejects_missing(self):
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        store.append_entries("a", [{"entry_id": "a-1", "date": _today(), "ts": _today_ts(8), "tag": "", "text": "x"}])
        self.assertTrue(store.tombstone("a-1", "a"))
        self.assertFalse(store.tombstone("a-1", "a"))
        self.assertFalse(store.tombstone("does-not-exist", "a"))

    def test_append_derives_date_from_timestamp_when_missing(self):
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        # 无 date 字段：由 ts 推导（用“今天”的 ts，避免被今日窗口拒绝）
        accepted, _ = store.append_entries("a", [{"entry_id": "a-1", "ts": _today_ts(8), "text": "x"}])
        self.assertIn("a-1", accepted)
        self.assertEqual(_today(), store.data["entries"]["a-1"]["date"])

    def test_append_rejects_missing_entry_id(self):
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        accepted, rejected = store.append_entries("a", [{"text": "no-id"}])
        self.assertEqual(accepted, [])
        self.assertEqual(rejected, [])
        self.assertEqual(0, store.data["version"])

    def test_append_normalizes_path_traversal_date(self):
        """date 含 `../` 会被规范为合法日期（由今天 ts 推导），避免渲染写出数据目录之外。"""
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        accepted, _ = store.append_entries(
            "a",
            [{"entry_id": "x-1", "date": "../../escape", "ts": _today_ts(8), "tag": "", "text": "hi"}],
        )
        self.assertIn("x-1", accepted)
        stored_date = store.data["entries"]["x-1"]["date"]
        self.assertEqual(_today(), stored_date)  # 非法 date 回退到今天（由 ts 推导）
        datetime.date.fromisoformat(stored_date)  # 必须可解析为合法日期

        records = _tmp_data_dir() / "Records"
        store.render_records(records)
        self.assertFalse((records.parent / "escape.md").exists())


class StoreSealPreviousDayTest(unittest.TestCase):
    """日界封存：state.json 只常驻今天的数据，历史日以整文件为准。"""

    def test_seal_purges_stale_dates_and_preserves_history_files(self):
        data = _tmp_data_dir() / "state.json"
        records_dir = data.parent / "Records"
        store = Store(data, records_dir)
        # 模拟“昨天”的条目已入库（进程运行到昨天），强制其 date=昨天
        store._today = _today()  # 无日界
        yesterday = (
            datetime.date.fromisoformat(_today()) - datetime.timedelta(days=1)
        ).isoformat()
        store.data["entries"]["y-1"] = {
            "entry_id": "y-1", "device_id": "a", "date": yesterday,
            "ts": _today_ts(8), "tag": "", "text": "昨日记录", "v": 1,
        }
        store.data["version"] = 1
        # 模拟进程重启落在“今天”：self._today 为 None，首次访问触发封存
        store._today = None

        # 触发封存：pull 会先 _maybe_seal_previous_day
        store.pull(0)

        self.assertNotIn("y-1", store.data["entries"])  # 昨日条目态已清理
        self.assertEqual(store.data["version"], 1)  # version 保持单调（不递增）
        # 昨日文件已在封存前落盘（render 一次），整文件保留
        self.assertTrue((records_dir / f"{yesterday}.md").exists())

    def test_seal_without_records_dir_keeps_entries_never_purges(self):
        """无 Records 落盘目录（配置错误兜底）：封存不清理历史条目态，避免数据丢失。

        Store 以 records_dir=None 构造时，若 _maybe_seal_previous_day 照常清理，
        会把 date < 今天 的条目清出 state.json，又因 _render_dates 无目录直接返回、
        从不落盘 —— 数据无处承载。这里应跳过封存并保留数据。
        """
        data = _tmp_data_dir() / "state.json"
        store = Store(data)  # records_dir 缺省 None
        store._today = _today()  # 无日界，便于注入“昨天”条目
        yesterday = (
            datetime.date.fromisoformat(_today()) - datetime.timedelta(days=1)
        ).isoformat()
        store.data["entries"]["y-1"] = {
            "entry_id": "y-1", "device_id": "a", "date": yesterday,
            "ts": _today_ts(8), "tag": "", "text": "昨日记录", "v": 1,
        }
        store.data["version"] = 1
        # 模拟进程重启落在“今天”：self._today 为 None，首次访问触发封存检查
        store._today = None

        store.pull(0)  # 内部会调用 _maybe_seal_previous_day

        # 无落盘目录 → 跳过封存：昨日条目仍在 state.json（不丢数据）
        self.assertIn("y-1", store.data["entries"])
        self.assertEqual(store.data["version"], 1)

    def test_device_names_derived_from_live_entries_only(self):
        """方案 B：设备名只是写在条目上的标签，服务端不单独记录设备清单。

        封存清理历史日条目态后，历史设备名随之消失（不再通过 seen_devices 额外缓存保留）。
        """
        data = _tmp_data_dir() / "state.json"
        # 传入 records_dir：封存先渲染历史日文件再清理条目态（真正验证“历史日
        # 设备名随条目态清理而消失”的语义；无落盘目录时封存会跳过清理、不丢数据）。
        store = Store(data, data.parent / "Records")
        store._today = _today()  # 无日界，便于注入“昨天”条目
        yesterday = (
            datetime.date.fromisoformat(_today()) - datetime.timedelta(days=1)
        ).isoformat()
        store.data["entries"]["y-1"] = {
            "entry_id": "y-1", "device_id": "MK8", "date": yesterday,
            "ts": _today_ts(8), "tag": "", "text": "昨日记录", "v": 1,
        }
        # 封存前：条目在态内，设备名可见
        self.assertIn("MK8", store.device_names())
        # 触发封存：清理 date < 今天 的条目态
        store._today = None
        store.pull(0)
        # 封存后：条目态被清理，设备名也不再保留（仅作条目标签，不单列）
        self.assertNotIn("MK8", store.device_names())


class StoreImmediateRenderTest(unittest.TestCase):
    """构造时传入 records_dir 后，写操作应立即渲染对应日期文件与 trash.md（即时落盘）。"""

    def test_append_entries_renders_date_immediately(self):
        data = _tmp_data_dir() / "state.json"
        records_dir = data.parent / "Records"
        store = Store(data, records_dir)

        store.append_entries(
            "a",
            [{"entry_id": "a-1", "date": _today(), "ts": _today_ts(8), "tag": "", "text": "即时正文"}],
        )

        target = records_dir / f"{_today()}.md"
        self.assertTrue(target.exists())
        self.assertIn("即时正文", target.read_text(encoding="utf-8"))
        self.assertEqual(store.data["version"], store.rendered_version())

    def test_tombstone_overwrites_date_file_immediately(self):
        data = _tmp_data_dir() / "state.json"
        records_dir = data.parent / "Records"
        store = Store(data, records_dir)
        store.append_entries(
            "a",
            [{"entry_id": "a-1", "date": _today(), "ts": _today_ts(8), "tag": "", "text": "将被删"}],
        )
        target = records_dir / f"{_today()}.md"
        self.assertIn("将被删", target.read_text(encoding="utf-8"))

        store.tombstone("a-1", "a")

        content = target.read_text(encoding="utf-8")
        self.assertNotIn("将被删", content)
        self.assertIn("myrecord-tombstone", content)
        trash = (records_dir / "trash.md").read_text(encoding="utf-8")
        self.assertIn("将被删", trash)

    def test_append_rejects_history_without_rendering(self):
        data = _tmp_data_dir() / "state.json"
        records_dir = data.parent / "Records"
        store = Store(data, records_dir)
        accepted, rejected = store.append_entries(
            "a",
            [{"entry_id": "old-1", "date": "2000-01-01", "ts": 946684800000, "tag": "", "text": "old"}],
        )
        self.assertEqual(rejected, ["old-1"])
        self.assertFalse((records_dir / "2000-01-01.md").exists())

    def test_no_render_when_records_dir_absent(self):
        data = _tmp_data_dir() / "state.json"
        store = Store(data)
        store.append_entries("a", [{"entry_id": "a-1", "date": _today(), "ts": _today_ts(8), "tag": "", "text": "x"}])
        self.assertFalse((data.parent / "Records" / f"{_today()}.md").exists())


class StoreDeleteTest(unittest.TestCase):
    """apply_deletes：客户端发起的 id 级删除（可离线），正文入垃圾桶、墓碑防复活。"""

    def _store(self, data):
        return Store(data, data.parent / "Records")

    def test_apply_deletes_tombstones_and_trashes_body(self):
        data = _tmp_data_dir() / "state.json"
        store = self._store(data)
        store.append_entries(
            "a", [{"entry_id": "a-1", "date": _today(), "ts": _today_ts(8), "tag": "", "text": "将被删"}]
        )
        accepted, rejected = store.apply_deletes(
            "b", [{"entry_id": "a-1", "date": _today(), "entry": {"ts": _today_ts(8)}}]
        )
        self.assertEqual(accepted, ["a-1"])
        self.assertEqual(rejected, [])
        self.assertNotIn("a-1", store.data["entries"])
        self.assertIn("a-1", store.data["tombstones"])
        self.assertEqual(store.data["trash"]["a-1"]["text"], "将被删")
        trash = (data.parent / "Records" / "trash.md").read_text(encoding="utf-8")
        self.assertIn("将被删", trash)

    def test_apply_deletes_unknown_id_with_body(self):
        """离线新建、从未推送就删除：服务端从未有过该条，凭 payload 正文进垃圾桶 + 墓碑。"""
        data = _tmp_data_dir() / "state.json"
        store = self._store(data)
        entry = {"entry_id": "ghost-1", "date": _today(), "ts": _today_ts(8), "tag": "", "text": "幽灵"}
        accepted, rejected = store.apply_deletes(
            "b", [{"entry_id": "ghost-1", "date": _today(), "entry": entry}]
        )
        self.assertEqual(accepted, ["ghost-1"])
        self.assertIn("ghost-1", store.data["tombstones"])
        self.assertEqual(store.data["trash"]["ghost-1"]["text"], "幽灵")

    def test_apply_deletes_rejects_history_date(self):
        data = _tmp_data_dir() / "state.json"
        store = self._store(data)
        accepted, rejected = store.apply_deletes(
            "b", [{"entry_id": "old-1", "date": "2000-01-01", "entry": {"ts": 1}}]
        )
        self.assertEqual(accepted, [])
        self.assertEqual(rejected, ["old-1"])
        self.assertEqual(store.data["tombstones"], {})

    def test_apply_deletes_idempotent(self):
        data = _tmp_data_dir() / "state.json"
        store = self._store(data)
        store.append_entries(
            "a", [{"entry_id": "a-1", "date": _today(), "ts": _today_ts(8), "tag": "", "text": "x"}]
        )
        item = {"entry_id": "a-1", "date": _today(), "entry": {"ts": _today_ts(8)}}
        store.apply_deletes("b", [item])
        accepted, _ = store.apply_deletes("b", [item])
        self.assertEqual(accepted, ["a-1"])
        self.assertEqual(len(store.data["tombstones"]), 1)


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
        self.assertTrue(resp.json()["ok"])

    def test_admin_model(self):
        resp = requests.post(
            f"{self.base}/api/admin/model",
            headers=self.headers,
            json={"name": "deepseek-v4-pro"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])


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
        resp = self._push_one({"entry_id": "a-1", "date": _today(), "ts": _today_ts(8), "tag": "", "text": "x"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["accepted"], ["a-1"])
        self.assertEqual(body["version"], 1)
        pull = requests.get(f"{self.base}/api/sync/pull?version=0", headers=self.headers)
        self.assertEqual(pull.status_code, 200)
        self.assertEqual(len(pull.json()["entries"]), 1)

    def test_push_rejects_history_date(self):
        """push 历史日条目 → 服务端 422 + rejected（双保险）。"""
        resp = self._push_one({"entry_id": "old-1", "date": "2000-01-01", "ts": 946684800000, "tag": "", "text": "old"})
        self.assertEqual(resp.status_code, 422)
        body = resp.json()
        self.assertEqual(body["error"], "expired")
        self.assertEqual(body["rejected"], ["old-1"])
        self.assertEqual(self.store.data["entries"], {})

    def test_push_with_deletes_tombstones_and_trashes(self):
        self._push_one({"entry_id": "a-1", "date": _today(), "ts": _today_ts(8), "tag": "", "text": "x"})
        resp = requests.post(
            f"{self.base}/api/sync/push",
            headers=self.headers,
            json={
                "entries": [],
                "deletes": [
                    {
                        "entry_id": "a-1",
                        "date": _today(),
                        "entry": {
                            "entry_id": "a-1",
                            "date": _today(),
                            "ts": _today_ts(8),
                            "tag": "",
                            "text": "x",
                        },
                    }
                ],
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("a-1", body["accepted"])
        self.assertEqual(self.store.data["entries"], {})
        self.assertIn("a-1", self.store.data["tombstones"])

    def test_push_rejects_expired_delete_batch(self):
        resp = requests.post(
            f"{self.base}/api/sync/push",
            headers=self.headers,
            json={"entries": [], "deletes": [{"entry_id": "old-1", "date": "2000-01-01"}]},
        )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["rejected"], ["old-1"])
        self.assertEqual(self.store.data["tombstones"], {})

    def test_unauthorized(self):
        bad = {"Authorization": "Bearer wrong", "X-Device-Id": self.device_id}
        resp = requests.get(f"{self.base}/api/sync/pull?version=0", headers=bad)
        self.assertEqual(resp.status_code, 401)

    def test_longpoll_fanout(self):
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
        self._push_one({"entry_id": "a-1", "date": _today(), "ts": _today_ts(8), "tag": "", "text": "x"})
        t.join(timeout=10)
        self.assertEqual(result["resp"].status_code, 200)
        self.assertEqual(len(result["resp"].json()["entries"]), 1)

    def test_status(self):
        self._push_one({"entry_id": "a-1", "date": _today(), "ts": _today_ts(8), "tag": "", "text": "x"})
        resp = requests.get(f"{self.base}/api/status", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["entry_count"], 1)
        self.assertIn(self.device_id, body["devices"])

    def test_failed_token_lockout(self):
        bad = {"Authorization": "Bearer nope", "X-Device-Id": self.device_id}
        for _ in range(5):
            requests.get(f"{self.base}/api/sync/pull?version=0", headers=bad)
        locked = requests.get(f"{self.base}/api/sync/pull?version=0", headers=self.headers)
        self.assertEqual(locked.status_code, 401)


class RecordsEndpointTest(unittest.TestCase):
    """GET /api/records（列表）与 /api/records/<name>（整文件回传，name=日期或 trash）。"""

    def setUp(self):
        self._dir = _tmp_data_dir()
        records_dir = self._dir / "Records"
        records_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self._dir / "state.json", records_dir)
        token = auth.new_token()
        self.device_id = self.store.register_device("records-test", token)
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

    def test_records_list_and_file(self):
        # 云端渲染今天 → 出现一个 Records/<today>.md
        self.store.append_entries(
            "a", [{"entry_id": "a-1", "date": _today(), "ts": _today_ts(8), "tag": "", "text": "今天内容"}]
        )
        # 历史日整文件（含旧格式）放在 Records 里，服务端原样回传
        records_dir = self._dir / "Records"
        records_dir.mkdir(parents=True, exist_ok=True)
        (records_dir / "2000-01-01.md").write_text(
            "# 2000-01-01\n\n旧格式 **08:00:** 历史内容\n", encoding="utf-8"
        )

        listed = requests.get(f"{self.base}/api/records", headers=self.headers)
        self.assertEqual(listed.status_code, 200)
        files = listed.json()["files"]
        self.assertEqual({f["date"] for f in files}, {_today(), "2000-01-01"})
        for f in files:
            self.assertEqual(len(f["sha256"]), 64)

        resp = requests.get(f"{self.base}/api/records/2000-01-01", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["Content-Type"].startswith("text/markdown"), True)
        self.assertIn("旧格式 **08:00:** 历史内容", resp.text)

    def test_records_file_rejects_bad_date(self):
        """非 YYYY-MM-DD 日期（路径穿越）被拒绝。"""
        for bad in ("../escape", "2024-01-01/../x", "2024-01-01.md"):
            resp = requests.get(f"{self.base}/api/records/{bad}", headers=self.headers)
            self.assertIn(resp.status_code, (400, 404))

    def test_records_file_missing_returns_404(self):
        resp = requests.get(f"{self.base}/api/records/2000-01-01", headers=self.headers)
        self.assertEqual(resp.status_code, 404)

    def test_records_list_keeps_unreadable_file_date(self):
        """不可解码的 .md 仍应列出该日期（sha256 为空），否则客户端会误删本地副本。"""
        records_dir = self._dir / "Records"
        (records_dir / "2000-01-01.md").write_text("# ok\n", encoding="utf-8")
        (records_dir / "2000-01-02.md").write_bytes(b"\xff\xfe\x00bad")
        listed = requests.get(f"{self.base}/api/records", headers=self.headers)
        self.assertEqual(listed.status_code, 200)
        files = {f["date"]: f["sha256"] for f in listed.json()["files"]}
        self.assertIn("2000-01-01", files)
        self.assertIn("2000-01-02", files)
        self.assertEqual("", files["2000-01-02"])

    def test_records_manifest_includes_trash_and_file_fetch(self):
        """单文件垃圾桶在 sha256 清单里单独一项，且可整文件拉取。"""
        self.store.append_entries(
            "a", [{"entry_id": "a-1", "date": _today(), "ts": _today_ts(8), "tag": "", "text": "待删正文"}]
        )
        self.store.tombstone("a-1", "a")  # 删除即渲染 Records/trash.md
        listed = requests.get(f"{self.base}/api/records", headers=self.headers)
        self.assertEqual(listed.status_code, 200)
        self.assertIn("trash", listed.json())
        self.assertEqual(len(listed.json()["trash"]), 64)
        # trash 不出现在日期 files 列表里（它不是日期名）
        self.assertNotIn("trash", {f["date"] for f in listed.json()["files"]})
        resp = requests.get(f"{self.base}/api/records/trash", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("待删正文", resp.text)

    def test_status_counts_follow_records_file(self):
        """/status 计数以今天 Records 文件为准：用户直接编辑文件后计数随之变化。"""
        self.store.append_entries(
            "a",
            [{"entry_id": "a-1", "date": _today(), "ts": _today_ts(8), "tag": "", "text": "原记录"}],
        )
        before = requests.get(f"{self.base}/api/status", headers=self.headers).json()
        self.assertEqual(before["entry_count"], 1)
        self.assertEqual(before["tombstone_count"], 0)

        # 绕过 state.json 直接编辑今天文件：追加一条新条目 + 一条墓碑。
        path = self._dir / "Records" / f"{_today()}.md"
        content = path.read_text(encoding="utf-8")
        content += "<!-- myrecord-time:manual-1 -->\n**09:00:** 手工补充\n\n"
        content += "<!-- myrecord-tombstone-time:a-1 -->\n"
        path.write_text(content, encoding="utf-8")

        after = requests.get(f"{self.base}/api/status", headers=self.headers).json()
        self.assertEqual(after["entry_count"], 2)
        self.assertEqual(after["tombstone_count"], 1)


if __name__ == "__main__":
    unittest.main()
