"""hub/server.py 的 version 语义测试（读端点 vs 写端点）。

设计基线 §4：version 是服务端全局单调递增游标。

- **读端点**（pull / longpoll）：version 是请求输入；缺失/为空回退 0（全量）；
  传入**非数字** version → 400。
- **写端点**（push / delete）：version 只是辅助游标，主操作不依赖它；
  非法/缺失回退 0（全量），保证 gap-free。

本模块用真实 ThreadingHTTPServer 走 HTTP 断言这些状态码与增量边界。
"""

import threading
import time
import unittest
from http.server import ThreadingHTTPServer

import requests

from server.hub import auth
from server.hub.server import serve
from server.hub.store import Store, today_utc8


def _ts(hour):
    import datetime
    _UTC8 = datetime.timezone(datetime.timedelta(hours=8))
    day = datetime.date.fromisoformat(today_utc8())
    return int(datetime.datetime(
        day.year, day.month, day.day, hour, 0, 0, tzinfo=_UTC8
    ).timestamp() * 1000)


class ServerVersionSemanticsTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "state.json")
        token = auth.new_token()
        self.device = self.store.register_device("ver-test", token)
        self.token = token
        self.httpd = serve(self.store, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.port}"
        self.h = {"Authorization": f"Bearer {token}", "X-Device-Id": self.device}

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._tmp.cleanup()

    def _push(self, entry, version=None):
        body = {"entries": [entry]}
        if version is not None:
            body["version"] = version
        return requests.post(f"{self.base}/api/sync/push", headers=self.h, json=body)

    # ---- 读端点：pull / longpoll ----

    def test_pull_missing_version_returns_full(self):
        self._push({"entry_id": "a-1", "date": today_utc8(), "ts": _ts(8), "text": "x"})
        r = requests.get(f"{self.base}/api/sync/pull", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["entries"]), 1, "缺失 version 应回退全量")

    def test_pull_empty_version_returns_full(self):
        self._push({"entry_id": "a-1", "date": today_utc8(), "ts": _ts(8), "text": "x"})
        r = requests.get(f"{self.base}/api/sync/pull?version=", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["entries"]), 1)

    def test_pull_non_numeric_version_returns_400(self):
        r = requests.get(f"{self.base}/api/sync/pull?version=abc", headers=self.h)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "bad version")

    def test_pull_zero_then_increment(self):
        # version=0 全量；再拉到最新 version 后 pull 应无新增
        self._push({"entry_id": "a-1", "date": today_utc8(), "ts": _ts(8), "text": "x"})
        r0 = requests.get(f"{self.base}/api/sync/pull?version=0", headers=self.h)
        v = r0.json()["version"]
        self.assertTrue(v >= 1)
        r_after = requests.get(f"{self.base}/api/sync/pull?version={v}", headers=self.h)
        self.assertEqual(r_after.json()["entries"], [])
        self.assertEqual(r_after.json()["tombstones"], [])

    def test_live_pull_returns_latest_version(self):
        self._push({"entry_id": "a-1", "date": today_utc8(), "ts": _ts(8), "text": "x"})
        r = requests.get(f"{self.base}/api/sync/pull?version=0", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["version"], self.store.data["version"])

    def test_longpoll_non_numeric_version_returns_400(self):
        r = requests.get(f"{self.base}/api/sync/longpoll?version=abc", headers=self.h)
        self.assertEqual(r.status_code, 400)

    # ---- 写端点：push / delete ----

    def test_push_missing_version_returns_full_gap_free_delta(self):
        self._push({"entry_id": "a-1", "date": today_utc8(), "ts": _ts(8), "text": "x"}, version=0)
        # 第二次 push 不带 version → 回退全量：应包含两条（a-1 与 b-1），无缺口
        r = self._push({"entry_id": "b-1", "date": today_utc8(), "ts": _ts(9), "text": "y"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        appeared = {e["entry_id"] for e in body.get("entries", [])}
        self.assertIn("a-1", appeared, "缺失 version 应回退全量，不得漏推 a-1")
        self.assertIn("b-1", appeared)

    def test_push_non_numeric_version_falls_back_to_full(self):
        # 写端点非法 version 不报 400，而是回退 0（全量）
        r = self._push(
            {"entry_id": "a-1", "date": today_utc8(), "ts": _ts(8), "text": "x"},
            version="garbage",
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("a-1", {e["entry_id"] for e in r.json()["entries"]})

    def test_delete_missing_version_returns_full_gap_free(self):
        self._push({"entry_id": "a-1", "date": today_utc8(), "ts": _ts(8), "text": "x"}, version=0)
        self._push({"entry_id": "b-1", "date": today_utc8(), "ts": _ts(9), "text": "y"}, version=0)
        r = requests.post(
            f"{self.base}/api/entries/delete", headers=self.h,
            json={"date": today_utc8()},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        # 删除产生墓碑；缺失 version 回退全量，应把全部增量（条目+墓碑）下发
        self.assertEqual(body["deleted"], "b-1")
        self.assertTrue(any(t["entry_id"] == "b-1" for t in body.get("tombstones", [])))

    def test_push_bad_request_when_entries_not_a_list(self):
        r = requests.post(f"{self.base}/api/sync/push", headers=self.h,
                          json={"entries": "not-a-list", "version": 0})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "bad request")


if __name__ == "__main__":
    unittest.main()
