"""hub/server.py 的同步令牌语义测试（令牌 = 今天文件 sha256）。

设计基线 §4：变更令牌不筛选数据，只回答「自上次询问后今天文件是否变了」：

- **读端点**（pull）：始终返回「今天」全量；令牌缺失/为空/任意值都回退全量。
- **长轮询**（longpoll）：令牌与当前今天文件哈希不一致即立即返回全量；一致则挂起至
  超时并返回空增量（仅带令牌）。
- **写端点**（push）：令牌仅作辅助，主操作不依赖它；写端点直接返回今天全量。

本模块用真实 ThreadingHTTPServer 走 HTTP 断言这些语义。
"""

import threading
import unittest

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


class ServerTokenSemanticsTest(unittest.TestCase):
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

    def _push(self, entry, token=None):
        body = {"entries": [entry]}
        if token is not None:
            body["token"] = token
        return requests.post(f"{self.base}/api/sync/push", headers=self.h, json=body)

    # ---- 读端点：pull / longpoll ----

    def test_pull_missing_token_returns_full(self):
        self._push({"entry_id": "a-1", "date": today_utc8(), "ts": _ts(8), "text": "x"})
        r = requests.get(f"{self.base}/api/sync/pull", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["entries"]), 1, "缺失 token 应回退全量")

    def test_pull_empty_token_returns_full(self):
        self._push({"entry_id": "a-1", "date": today_utc8(), "ts": _ts(8), "text": "x"})
        r = requests.get(f"{self.base}/api/sync/pull?token=", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["entries"]), 1)

    def test_pull_always_returns_full_today(self):
        # 令牌不筛选数据：无论传什么 token，pull 都返回今天全量（客户端按 id 幂等合并）。
        self._push({"entry_id": "a-1", "date": today_utc8(), "ts": _ts(8), "text": "x"})
        r0 = requests.get(f"{self.base}/api/sync/pull?token=0", headers=self.h)
        self.assertEqual([e["entry_id"] for e in r0.json()["entries"]], ["a-1"])
        token = r0.json()["token"]
        r_after = requests.get(f"{self.base}/api/sync/pull?token={token}", headers=self.h)
        self.assertEqual([e["entry_id"] for e in r_after.json()["entries"]], ["a-1"])

    def test_live_pull_returns_current_file_token(self):
        self._push({"entry_id": "a-1", "date": today_utc8(), "ts": _ts(8), "text": "x"})
        r = requests.get(f"{self.base}/api/sync/pull?token=0", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["token"], self.store.today_state()["token"])
        self.assertEqual(len(r.json()["token"]), 64)

    # ---- 写端点：push ----

    def test_push_missing_token_returns_full_gap_free_delta(self):
        self._push({"entry_id": "a-1", "date": today_utc8(), "ts": _ts(8), "text": "x"}, token="")
        # 第二次 push 不带 token → 回退全量：应包含两条（a-1 与 b-1），无缺口
        r = self._push({"entry_id": "b-1", "date": today_utc8(), "ts": _ts(9), "text": "y"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        appeared = {e["entry_id"] for e in body.get("entries", [])}
        self.assertIn("a-1", appeared, "缺失 token 应回退全量，不得漏推 a-1")
        self.assertIn("b-1", appeared)

    def test_push_bad_request_when_entries_not_a_list(self):
        r = requests.post(f"{self.base}/api/sync/push", headers=self.h,
                          json={"entries": "not-a-list", "token": "0"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "bad request")


if __name__ == "__main__":
    unittest.main()
