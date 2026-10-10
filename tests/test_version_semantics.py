"""hub/server.py 的同步令牌语义测试（游标已降级为「变更令牌 revision」）。

设计基线 §4：``version`` 不再是数据筛选游标，而是服务端单调递增的**变更令牌**：

- **读端点**（pull）：始终返回「今天」全量（游标不参与筛选）；缺失/为空回退 0，
  传入**非数字** version 仍返回 400（无法解析为合法令牌）。
- **长轮询**（longpoll）：令牌是「自上次询问后变了没」的输入；非数字 → 400；
  令牌落后即有变化则立即返回今天全量，无变化则挂起至超时并返回空增量。
- **写端点**（push / delete）：令牌仅作辅助，主操作不依赖它；写端点直接返回今天全量。

本模块用真实 ThreadingHTTPServer 走 HTTP 断言这些状态码与全量语义。
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

    def test_pull_always_returns_full_today(self):
        # 游标降级：无论传什么 version，pull 都返回今天全量（客户端按 id 幂等合并）
        self._push({"entry_id": "a-1", "date": today_utc8(), "ts": _ts(8), "text": "x"})
        r0 = requests.get(f"{self.base}/api/sync/pull?version=0", headers=self.h)
        self.assertEqual([e["entry_id"] for e in r0.json()["entries"]], ["a-1"])
        v = r0.json()["version"]
        r_after = requests.get(f"{self.base}/api/sync/pull?version={v}", headers=self.h)
        self.assertEqual([e["entry_id"] for e in r_after.json()["entries"]], ["a-1"])

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

    def test_push_bad_request_when_entries_not_a_list(self):
        r = requests.post(f"{self.base}/api/sync/push", headers=self.h,
                          json={"entries": "not-a-list", "version": 0})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "bad request")


if __name__ == "__main__":
    unittest.main()
