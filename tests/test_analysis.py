import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.ai import settings
from server.ai.ai_client import AIResponse
from server.ai.analysis import context, orchestrator


class LegacyReadTests(unittest.TestCase):
    """旧数据向后兼容：AI 读取正文时不得把旧 agentrecord-* 标记混入文本。"""

    def test_period_records_ignore_old_markers(self):
        old = (
            "# 2024-01-01\n\n<summary>\n暂无今日总结。\n</summary>\n\n---\n"
            "## 原始记录流\n\n"
            "<!-- agentrecord-record -->\n**08:00:** 今天去爬山\n\n"
            "<!-- agentrecord-record-text -->\n**09:00 [引用]:** 引用了一篇文档\n"
        )
        records = context._period_records([("2024-01-01", old)])
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertNotIn("<!--", record["text"])
            self.assertNotIn("agentrecord", record["text"])
        self.assertEqual(records[0]["tag"], "")
        self.assertEqual(records[1]["tag"], "[引用]")


class AnalysisWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.original_diary = settings.DIARY_DIR
        self.original_analysis = settings.ANALYSIS_DIR
        self.original_automation = settings.CONFIG.get("automation")
        self.original_call_ai = orchestrator.call_ai
        settings.DIARY_DIR = root / "Records"
        settings.ANALYSIS_DIR = root / "AnalysisReports"
        settings.DIARY_DIR.mkdir()
        settings.CONFIG["automation"] = {
            "enabled": True,
            "daily_summary": True,
            "weekly_report": False,
            "monthly_report": False,
        }
        self.ai_calls = []
        orchestrator.call_ai = self.fake_call_ai

    def tearDown(self):
        settings.DIARY_DIR = self.original_diary
        settings.ANALYSIS_DIR = self.original_analysis
        settings.CONFIG.pop("automation", None)
        orchestrator.call_ai = self.original_call_ai
        self.temp_dir.cleanup()

    def fake_call_ai(self, prompt, model_cfg, **kwargs):
        self.ai_calls.append(prompt)
        if "[程序 Agent 任务:" not in prompt:
            return "测试总结", True
        if "任务:report]" in prompt:
            return AIResponse(
                "## 本周回顾\n- 完成记录模块重构。[1]\n\n## 遇到的问题\n- 修复同步丢帧。[2]",
                True,
                {
                    "usage": {
                        "prompt_tokens": 7,
                        "completion_tokens": 3,
                        "total_tokens": 10,
                        "cached_tokens": 2,
                        "cache_miss_tokens": 5,
                    }
                },
            )
        raise AssertionError("不再存在其他 Agent：reviewer / retrospective 已取消")

    def write_diary(self, date: str):
        path = settings.DIARY_DIR / f"{date}.md"
        raw = "**09:00:** 我开始重视记录是否可以验证。\n\n"
        path.write_text(
            f"# {date}\n\n<summary>\n旧总结\n</summary>\n\n---\n## 原始记录流\n\n{raw}",
            encoding="utf-8",
        )
        return path

    # ---- 每日总结 ----

    def test_summary_only_replaces_summary_region(self):
        diary = self.write_diary("2026-07-14")
        _, success = orchestrator.summarize_diary("2026-07-14", {"name": "mock"})
        self.assertTrue(success)
        content = diary.read_text(encoding="utf-8")
        self.assertIn("<summary>\n测试总结\n</summary>", content)
        self.assertIn("我开始重视记录是否可以验证", content)

    def test_summary_removes_one_outer_wrapper_and_leading_heading(self):
        diary = self.write_diary("2026-07-14")
        wrapped = ("```markdown\n<summary>\n# 日记总结\n有效正文\n</summary>\n```")
        with patch.object(
            orchestrator, "call_ai", return_value=AIResponse(wrapped, True)
        ) as call_model:
            summary, success = orchestrator.summarize_diary(
                "2026-07-14", {"name": "mock"}
            )

        self.assertTrue(success)
        self.assertEqual("有效正文", summary)
        self.assertIn("<summary>\n有效正文\n</summary>", diary.read_text(encoding="utf-8"))
        self.assertEqual(1, call_model.call_count)

    def test_summary_format_error_gets_configured_bounded_revision(self):
        self.write_diary("2026-07-14")
        responses = iter(
            [
                AIResponse("正文\n## 不应保留的小节\n内容", True),
                AIResponse("修订后的连续正文", True),
            ]
        )
        prompts = []

        def reply(prompt, *_args, **_kwargs):
            prompts.append(prompt)
            return next(responses)

        with patch.dict(
            settings.CONFIG, {"retry": {"daily_summary_retry_limit": 1}}
        ), patch.object(orchestrator, "call_ai", side_effect=reply):
            summary, success = orchestrator.summarize_diary(
                "2026-07-14", {"name": "mock"}
            )

        self.assertTrue(success)
        self.assertEqual("修订后的连续正文", summary)
        self.assertIn("中控格式修正", prompts[1])
        self.assertIn("正文内标题", prompts[1])

    def test_summary_rejects_json_after_retry_limit(self):
        diary = self.write_diary("2026-07-14")
        original = diary.read_text(encoding="utf-8")
        with patch.dict(
            settings.CONFIG, {"retry": {"daily_summary_retry_limit": 0}}
        ), patch.object(
            orchestrator,
            "call_ai",
            return_value=AIResponse('{"summary":"正文"}', True),
        ):
            _, success = orchestrator.summarize_diary(
                "2026-07-14", {"name": "mock"}
            )

        self.assertFalse(success)
        self.assertEqual(original, diary.read_text(encoding="utf-8"))

    def test_summary_does_not_write_when_diary_changes_during_model_call(self):
        diary = self.write_diary("2026-07-14")

        def mutate_then_reply(*args, **kwargs):
            diary.write_text(
                diary.read_text(encoding="utf-8") + "**10:00:** 模型调用期间新增\n",
                encoding="utf-8",
            )
            return AIResponse("过时总结", True)

        with patch.object(orchestrator, "call_ai", side_effect=mutate_then_reply):
            _, success = orchestrator.summarize_diary(
                "2026-07-14", {"name": "mock"}
            )

        self.assertFalse(success)
        self.assertNotIn("过时总结", diary.read_text(encoding="utf-8"))

    # ---- 周报 / 月报（单次 Report Agent，不校验）----

    def test_weekly_report_is_single_pass_with_citation(self):
        day = datetime.date(2026, 7, 14)
        diary = self.write_diary(day.isoformat())
        original = diary.read_bytes()
        _, success, path = orchestrator.generate_analysis_report(
            "weekly", day, {"name": "mock"}
        )
        self.assertTrue(success)
        content = path.read_text(encoding="utf-8")
        self.assertIn("分析周报", content)
        self.assertIn("使用模型：mock", content)
        self.assertIn("Token 用量", content)
        self.assertIn("本周回顾", content)
        self.assertIn("[1]", content)
        self.assertEqual(original, diary.read_bytes())

    def test_weekly_report_is_single_report_agent_no_reviewer(self):
        day = datetime.date(2026, 7, 14)
        self.write_diary(day.isoformat())
        _, success, _ = orchestrator.generate_analysis_report(
            "weekly", day, {"name": "mock"}
        )
        self.assertTrue(success)
        report_calls = [p for p in self.ai_calls if "[程序 Agent 任务:report]" in p]
        self.assertEqual(1, len(report_calls))
        self.assertFalse(any("reviewer" in p for p in self.ai_calls))
        self.assertFalse(any("retrospective" in p for p in self.ai_calls))

    def test_weekly_report_input_labels_records_with_date_and_line(self):
        day = datetime.date(2026, 7, 14)
        self.write_diary(day.isoformat())
        _, success, path = orchestrator.generate_analysis_report(
            "weekly", day, {"name": "mock"}
        )
        self.assertTrue(success)
        report_prompt = next(
            p for p in self.ai_calls if "[程序 Agent 任务:report]" in p
        )
        self.assertIn("2026-07-14", report_prompt)  # 按日期分隔
        self.assertIn("line", report_prompt)        # 行号标注
        self.assertIn("\"n\": 1", report_prompt)   # 全局引用编号
        content = path.read_text(encoding="utf-8")
        self.assertNotIn("记录依据", content)        # 不再有旧页脚
        self.assertNotIn("R-2026", content)

    def test_weekly_report_without_records_fails_cleanly(self):
        day = datetime.date(2026, 7, 14)
        message, success, path = orchestrator.generate_analysis_report(
            "weekly", day, {"name": "mock"}
        )
        self.assertFalse(success)
        self.assertIsNone(path)
        self.assertIn("没有日记记录", message)

    def test_monthly_report_single_pass_reads_only_period_records(self):
        day = datetime.date(2026, 7, 14)
        self.write_diary(day.isoformat())
        _, success, path = orchestrator.generate_analysis_report(
            "monthly", day, {"name": "mock"}
        )
        self.assertTrue(success)
        report_prompt = next(
            p for p in self.ai_calls if "[程序 Agent 任务:report]" in p
        )
        self.assertIn("2026-07-14", report_prompt)
        self.assertNotIn("周报", report_prompt)  # 不读取周期周报/历史报告
        content = path.read_text(encoding="utf-8")
        self.assertIn("分析月报", content)

    def test_monthly_report_does_not_reuse_weekly_report(self):
        week_anchor = datetime.date(2026, 7, 14)
        self.write_diary(week_anchor.isoformat())
        _, weekly_ok, _ = orchestrator.generate_analysis_report(
            "weekly", week_anchor, {"name": "mock"}
        )
        self.assertTrue(weekly_ok)
        self.ai_calls.clear()

        month_anchor = datetime.date(2026, 7, 20)
        self.write_diary(month_anchor.isoformat())
        _, monthly_ok, path = orchestrator.generate_analysis_report(
            "monthly", month_anchor, {"name": "mock"}
        )
        self.assertTrue(monthly_ok)
        report_prompt = next(
            p for p in self.ai_calls if "[程序 Agent 任务:report]" in p
        )
        self.assertNotIn("周报", report_prompt)  # 月报不再复用旧周报板块
        self.assertIn("分析月报", path.read_text(encoding="utf-8"))

    def test_report_has_no_reviewer_or_revision_loop(self):
        day = datetime.date(2026, 7, 14)
        self.write_diary(day.isoformat())
        _, success, _ = orchestrator.generate_analysis_report(
            "weekly", day, {"name": "mock"}
        )
        self.assertTrue(success)  # 单次即成功，无审查/修订循环
        self.assertFalse(any("reviewer" in p for p in self.ai_calls))
        self.assertFalse(any("修订" in p for p in self.ai_calls))


if __name__ == "__main__":
    unittest.main()