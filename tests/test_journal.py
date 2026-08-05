import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from AgentRecord import journal, settings


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
        old.write_text("旧日记", encoding="utf-8")
        latest.write_text("新日记", encoding="utf-8")
        sources = journal.list_reference_sources()
        filtered = journal.list_reference_sources("2026-07-13")
        self.assertEqual(("日记 | 2026-07-14", latest), sources[0])
        self.assertEqual([("日记 | 2026-07-13", old)], filtered)
        self.assertEqual([], journal.list_reference_sources("2026-06"))

    def test_appends_portable_reference_with_note_and_timestamp(self):
        report = settings.DIARY_DIR / "2026-07-14.md"
        report.write_text("日记", encoding="utf-8")
        label = "日记 | 2026-07-14"
        fixed_now = datetime.datetime(2026, 7, 15, 14, 32)

        with patch("AgentRecord.journal.datetime.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            journal.append_reference(label, report, "继续展开的想法")

        content = (settings.DIARY_DIR / "2026-07-15.md").read_text(encoding="utf-8")
        self.assertIn("**14:32 [引用]:**", content)
        self.assertIn(f"[{label}](<{report.name}>)", content)
        self.assertIn("继续展开的想法", content)

    def test_plain_record_uses_one_submission_time_across_midnight(self):
        submitted_at = datetime.datetime(2026, 7, 15, 23, 59, 59)
        after_midnight = datetime.datetime(2026, 7, 16, 0, 0, 0)

        with patch("AgentRecord.journal.datetime.datetime") as mock_datetime:
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

        with patch("AgentRecord.journal.datetime.datetime") as mock_datetime:
            mock_datetime.now.side_effect = [submitted_at, after_midnight]
            journal.append_reference("日记 | 2026-07-14", report, "跨午夜引用")

        mock_datetime.now.assert_called_once_with()
        content = (settings.DIARY_DIR / "2026-07-15.md").read_text(encoding="utf-8")
        self.assertIn("**23:59 [引用]:**", content)
        self.assertIn("跨午夜引用", content)
        self.assertFalse((settings.DIARY_DIR / "2026-07-16.md").exists())

    def test_delete_last_record_removes_multiline_reference_only(self):
        fixed_now = datetime.datetime(2026, 7, 15, 9, 0)
        with patch("AgentRecord.journal.datetime.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            journal.append_log("先前记录")
            journal.append_log("[日记 | 2026-07-14](<2026-07-14.md>)\n\n关联想法", "[引用]")

            self.assertTrue(journal.delete_last_record())
        content = (settings.DIARY_DIR / "2026-07-15.md").read_text(encoding="utf-8")
        self.assertIn("先前记录", content)
        self.assertNotIn("关联想法", content)

    def test_fake_timestamp_inside_multiline_record_is_not_a_record_boundary(self):
        fixed_now = datetime.datetime(2026, 7, 15, 9, 0)
        with patch("AgentRecord.journal.datetime.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            journal.append_log("第一条\n**10:15:** 这是内容，不是新记录")
            journal.append_log("第二条")
            self.assertTrue(journal.delete_last_record())

        path = settings.DIARY_DIR / "2026-07-15.md"
        content = path.read_text(encoding="utf-8")
        self.assertIn("**10:15:** 这是内容，不是新记录", content)
        self.assertNotIn("第二条", content)

        from AgentRecord.analysis.context import _period_records

        records = _period_records([("2026-07-15", content)])
        self.assertEqual(1, len(records))
        self.assertIn("**10:15:**", records[0]["text"])

    def test_literal_record_marker_is_content_and_last_record_still_deletes(self):
        fixed_now = datetime.datetime(2026, 7, 15, 9, 0)
        with patch("AgentRecord.journal.datetime.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            journal.append_log(
                f"标记前\n{journal.RECORD_MARKER}\n标记后"
            )
            journal.append_log("第二条")

        path = settings.DIARY_DIR / "2026-07-15.md"
        content = path.read_text(encoding="utf-8")
        self.assertIn(journal.ESCAPED_RECORD_MARKER, content)

        from AgentRecord.analysis.context import _period_records

        records = _period_records([("2026-07-15", content)])
        self.assertEqual(2, len(records))
        self.assertIn(journal.RECORD_MARKER, records[0]["text"])
        with patch("AgentRecord.journal.datetime.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            self.assertTrue(journal.delete_last_record())
        remaining = _period_records([("2026-07-15", path.read_text(encoding="utf-8"))])
        self.assertEqual(1, len(remaining))
        self.assertIn("标记后", remaining[0]["text"])

    def test_record_source_id_changes_when_same_position_content_changes(self):
        from AgentRecord.analysis.context import _period_records

        first = _period_records([("2026-07-15", "**09:00:** 原内容")])[0]
        unchanged = _period_records([("2026-07-15", "**09:00:** 原内容")])[0]
        changed = _period_records([("2026-07-15", "**09:00:** 新内容")])[0]

        self.assertEqual(first["source_id"], unchanged["source_id"])
        self.assertNotEqual(first["source_id"], changed["source_id"])
        self.assertRegex(first["source_id"], r"^R-20260715-001-[0-9a-f]{12}$")

    def test_tool_date_cannot_escape_diary_directory(self):
        message = journal.read_daily_log(date="../Docs/设计原则与系统架构")

        self.assertIn("YYYY-MM-DD", message)


if __name__ == "__main__":
    unittest.main()
