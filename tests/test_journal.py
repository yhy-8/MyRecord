import tempfile
import unittest
from pathlib import Path

from server.ai import settings
from server.ai.analysis.context import _period_records
from server.ai import journal


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.original_diary_dir = settings.DIARY_DIR
        self.original_analysis_dir = settings.ANALYSIS_DIR
        settings.DIARY_DIR = root / "Records"
        settings.ANALYSIS_DIR = root / "AnalysisReports"
        settings.DIARY_DIR.mkdir()

    def tearDown(self):
        settings.DIARY_DIR = self.original_diary_dir
        settings.ANALYSIS_DIR = self.original_analysis_dir
        self.temp_dir.cleanup()

    def test_record_source_id_is_date_line_based(self):
        # source_id = R-日期-行号（按位置定位、与内容无关），行号为日期文件中 1-based 行号。
        first = _period_records([("2026-07-15", "**09:00:** 原内容")])[0]
        changed = _period_records([("2026-07-15", "**09:00:** 新内容")])[0]
        second_line = _period_records(
            [("2026-07-15", "**09:00:** A\n\n**10:00:** B")]
        )[1]

        self.assertEqual("R-20260715-1", first["source_id"])
        self.assertEqual(1, first["line"])
        self.assertEqual(first["source_id"], changed["source_id"])  # 位置决定，不随内容变
        self.assertEqual("R-20260715-3", second_line["source_id"])  # 第 3 行
        self.assertEqual(3, second_line["line"])

    def test_summary_replacement_preserves_backslashes_literally(self):
        path = settings.DIARY_DIR / "2026-07-15.md"
        path.write_text(
            "# 2026-07-15\n\n<summary>\n旧总结\n</summary>\n\n**09:00:** 内容\n",
            encoding="utf-8",
        )
        summary = r"路径 C:\Users\name；引用 \1；正则 \d+"

        message = journal.update_summary_for_date("2026-07-15", summary)

        self.assertIn("已写入", message)
        self.assertIn(summary, path.read_text(encoding="utf-8"))

    def test_summary_rejects_stale_source_hash(self):
        path = settings.DIARY_DIR / "2026-07-15.md"
        path.write_text(
            "# 2026-07-15\n\n<summary>\n旧总结\n</summary>\n",
            encoding="utf-8",
        )

        message = journal.update_summary_for_date(
            "2026-07-15", "新总结", expected_content_hash="stale"
        )

        self.assertIn("发生变化", message)
        self.assertNotIn("新总结", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
