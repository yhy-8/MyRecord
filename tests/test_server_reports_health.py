"""hub/server.py 的 health / reports 端点测试（现有套件未覆盖）。

- GET /api/health：无需鉴权的存活探针（客户端探测用）
- GET /api/reports：报告哈希清单（rel + sha256），支持 kind 过滤（weekly/monthly）
- GET /api/reports/<kind>/<name>：报告整文件回传（只读）
- 报告路径穿越防护：../../ 越界相对路径 → 404
"""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from server.hub import auth
from server.hub.server import serve
from server.hub.store import Store


def _tmp():
    return Path(tempfile.mkdtemp(prefix="myrecord-reports-"))


class ReportsEndpointTest(unittest.TestCase):
    def setUp(self):
        self._dir = _tmp()
        self.store = Store(self._dir / "state.json")
        token = auth.new_token()
        self.device = self.store.register_device("report-test", token)
        self.token = token

        base = self._dir / "AnalysisReports"
        weekly = base / "Weekly"
        monthly = base / "Monthly"
        weekly.mkdir(parents=True, exist_ok=True)
        monthly.mkdir(parents=True, exist_ok=True)
        (weekly / "2026-07-06_to_2026-07-12.md").write_text("# 周报内容\n正文", encoding="utf-8")
        (monthly / "2026-07.md").write_text("# 月报内容\n正文", encoding="utf-8")

        def list_reports(kind):
            prefix = (base / (kind or "")).resolve()
            if not prefix.is_relative_to(base.resolve()) or not prefix.is_dir():
                return []
            return sorted(
                str(p.relative_to(base.resolve()))
                for p in prefix.rglob("*.md")
            )

        def read_report(rel):
            target = (base / rel).resolve()
            if not target.is_relative_to(base.resolve()) or not target.is_file():
                return None
            return target.read_text(encoding="utf-8")

        self.httpd = serve(
            self.store, "127.0.0.1", 0,
            list_reports=list_reports, read_report=read_report,
        )
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.port}"
        self.h = {"Authorization": f"Bearer {token}", "X-Device-Id": self.device}

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_health_no_auth(self):
        r = requests.get(f"{self.base}/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_reports_list_has_sha256_and_rel(self):
        r = requests.get(f"{self.base}/api/reports", headers=self.h)
        self.assertEqual(r.status_code, 200)
        files = r.json()["files"]
        rels = {f["rel"] for f in files}
        self.assertEqual({"Weekly/2026-07-06_to_2026-07-12.md", "Monthly/2026-07.md"}, rels)
        for f in files:
            self.assertEqual(64, len(f["sha256"]))

    def test_reports_list_kind_filter_weekly(self):
        r = requests.get(f"{self.base}/api/reports?kind=Weekly", headers=self.h)
        rels = {f["rel"] for f in r.json()["files"]}
        self.assertEqual({"Weekly/2026-07-06_to_2026-07-12.md"}, rels)

    def test_reports_list_kind_filter_monthly(self):
        r = requests.get(f"{self.base}/api/reports?kind=Monthly", headers=self.h)
        rels = {f["rel"] for f in r.json()["files"]}
        self.assertEqual({"Monthly/2026-07.md"}, rels)

    def test_reports_list_unknown_kind_returns_empty(self):
        r = requests.get(f"{self.base}/api/reports?kind=nope", headers=self.h)
        self.assertEqual([], r.json()["files"])

    def test_report_file_returns_markdown(self):
        r = requests.get(
            f"{self.base}/api/reports/Weekly/2026-07-06_to_2026-07-12.md",
            headers=self.h,
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["Content-Type"].startswith("text/markdown"))
        self.assertIn("# 周报内容", r.text)

    def test_report_file_path_traversal_rejected(self):
        for bad in ("../outside.md", "Weekly/../../x.md", "../../../etc/passwd"):
            r = requests.get(f"{self.base}/api/reports/{bad}", headers=self.h)
            self.assertIn(r.status_code, (404, 400))

    def test_report_file_missing_returns_404(self):
        r = requests.get(f"{self.base}/api/reports/Monthly/2099-01.md", headers=self.h)
        self.assertEqual(r.status_code, 404)

    def test_reports_requires_auth(self):
        r = requests.get(f"{self.base}/api/reports")
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
