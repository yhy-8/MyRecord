import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.ai import journal, settings
from server.ai.analysis.context import _period_records


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

    def test_lists_only_diaries_as_reference_sources(self):
        old = settings.DIARY_DIR / "2026-07-13.md"
        latest = settings.DIARY_DIR / "2026-07-14.md"
        unrelated = settings.DIARY_DIR / "notes.md"
        old.write_text("旧日记", encoding="utf-8")
        latest.write_text("新日记", encoding="utf-8")
        unrelated.write_text("不是日期日记", encoding="utf-8")
        sources = journal.list_reference_sources()
        filtered = journal.list_reference_sources("2026-07-13")
        self.assertEqual(("日记 | 2026-07-14", latest), sources[0])
        self.assertEqual([("日记 | 2026-07-13", old)], filtered)
        self.assertEqual([], journal.list_reference_sources("2026-06"))
        self.assertNotIn(unrelated, [path for _, path in sources])

    def test_short_leap_day_resolves_in_a_leap_year(self):
        class LeapDate(datetime.date):
            @classmethod
            def today(cls):
                return cls(2028, 2, 1)

        with patch.object(journal.datetime, "date", LeapDate):
            self.assertEqual("2028-02-29", journal.resolve_date("02-29"))

    def test_appends_portable_reference_with_note_and_timestamp(self):
        report = settings.DIARY_DIR / "2026-07-14.md"
        report.write_text("日记", encoding="utf-8")
        label = "日记 | 2026-07-14"
        fixed_now = datetime.datetime(2026, 7, 15, 14, 32)

        with patch("server.ai.journal.datetime.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            journal.append_reference(label, report, "继续展开的想法")

        content = (settings.DIARY_DIR / "2026-07-15.md").read_text(encoding="utf-8")
        self.assertIn("**14:32 [引用]:**", content)
        self.assertIn(f"[{label}](<{report.name}>)", content)
        self.assertIn("继续展开的想法", content)

    def test_plain_record_uses_one_submission_time_across_midnight(self):
        submitted_at = datetime.datetime(2026, 7, 15, 23, 59, 59)
        after_midnight = datetime.datetime(2026, 7, 16, 0, 0, 0)

        with patch("server.ai.journal.datetime.datetime") as mock_datetime:
            mock_datetime.now.side_effect = [submitted_at, after_midnight]
            journal.append_log("跨午夜提交")

        mock_datetime.now.assert_called_once_with()
        content = (settings.DIARY_DIR / "2026-07-15.md").read_text(encoding="utf-8")
        self.assertTrue(content.startswith("# 2026-07-15\n"))
        self.assertIn("**23:59:** 跨午夜提交", content)
        self.assertFalse((settings.DIARY_DIR / "2026-07-16.md").exists())

    def test_reference_uses_one_submission_time_across_midnight(self):
        report = settings.DIARY_DIR / "2026-07-14.md"
        report.write_text("月报", encoding="utf-8")
        submitted_at = datetime.datetime(2026, 7, 15, 23, 59, 59)
        after_midnight = datetime.datetime(2026, 7, 16, 0, 0, 0)

        with patch("server.ai.journal.datetime.datetime") as mock_datetime:
            mock_datetime.now.side_effect = [submitted_at, after_midnight]
            journal.append_reference("日记 | 2026-07-14", report, "跨午夜引用")

        mock_datetime.now.assert_called_once_with()
        content = (settings.DIARY_DIR / "2026-07-15.md").read_text(encoding="utf-8")
        self.assertIn("**23:59 [引用]:**", content)
        self.assertIn("跨午夜引用", content)
        self.assertFalse((settings.DIARY_DIR / "2026-07-16.md").exists())

    def test_delete_last_record_removes_multiline_reference_only(self):
        fixed_now = datetime.datetime(2026, 7, 15, 9, 0)
        with patch("server.ai.journal.datetime.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            journal.append_log("先前记录")
            journal.append_log("[日记 | 2026-07-14](<2026-07-14.md>)\n\n关联想法", "[引用]")

            self.assertTrue(journal.delete_last_record())
        content = (settings.DIARY_DIR / "2026-07-15.md").read_text(encoding="utf-8")
        self.assertIn("先前记录", content)
        self.assertNotIn("关联想法", content)

    def test_fake_timestamp_inside_multiline_record_is_not_a_record_boundary(self):
        fixed_now = datetime.datetime(2026, 7, 15, 9, 0)
        with patch("server.ai.journal.datetime.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            journal.append_log("第一条\n**10:15:** 这是内容，不是新记录")
            journal.append_log("第二条")
            self.assertTrue(journal.delete_last_record())

        path = settings.DIARY_DIR / "2026-07-15.md"
        content = path.read_text(encoding="utf-8")
        self.assertIn("**10:15:** 这是内容，不是新记录", content)
        self.assertNotIn("第二条", content)

        records = _period_records([("2026-07-15", content)])
        self.assertEqual(1, len(records))
        self.assertIn("**10:15:**", records[0]["text"])

    def test_literal_record_marker_is_content_and_last_record_still_deletes(self):
        fixed_now = datetime.datetime(2026, 7, 15, 9, 0)
        with patch("server.ai.journal.datetime.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            journal.append_log(
                f"标记前\n{journal.RECORD_MARKER}\n标记后"
            )
            journal.append_log("第二条")

        path = settings.DIARY_DIR / "2026-07-15.md"
        content = path.read_text(encoding="utf-8")
        self.assertIn(journal.ESCAPED_RECORD_MARKER, content)

        records = _period_records([("2026-07-15", content)])
        self.assertEqual(2, len(records))
        self.assertIn(journal.RECORD_MARKER, records[0]["text"])
        with patch("server.ai.journal.datetime.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            self.assertTrue(journal.delete_last_record())
        remaining = _period_records([("2026-07-15", path.read_text(encoding="utf-8"))])
        self.assertEqual(1, len(remaining))
        self.assertIn("标记后", remaining[0]["text"])

    def test_record_source_id_is_date_line_based(self):
        # 新设计：source_id = R-日期-行号（按位置定位、与内容无关），行号为日期文件中 1-based 行号。
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