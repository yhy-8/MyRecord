import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from AgentRecord import settings
from AgentRecord.ai_client import AIResponse, ToolResult
from AgentRecord.analysis import automation, context, orchestrator
from AgentRecord.analysis.store import AnalysisStore
from AgentRecord.agents.base import AgentPipelineError


class AnalysisWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.original_diary = settings.DIARY_DIR
        self.original_analysis = settings.ANALYSIS_DIR
        self.original_automation = settings.CONFIG.get("automation")
        self.original_third_search = settings.CONFIG.get("third_search")
        self.original_call_ai = orchestrator.call_ai
        self.original_search_web_once = orchestrator.search_web_once
        settings.DIARY_DIR = root / "Records"
        settings.ANALYSIS_DIR = root / "AnalysisReports"
        settings.DIARY_DIR.mkdir()
        settings.CONFIG["automation"] = {
            "enabled": True,
            "daily_summary": True,
            "weekly_report": False,
            "monthly_report": False,
        }
        settings.CONFIG["third_search"] = {
            "enabled": True,
            "api_key": "test-key",
            "api_url": "https://search.example.test",
        }
        self.ai_calls = []
        orchestrator.call_ai = self.fake_call_ai
        orchestrator.search_web_once = self.fake_search_web_once

    def tearDown(self):
        settings.DIARY_DIR = self.original_diary
        settings.ANALYSIS_DIR = self.original_analysis
        orchestrator.call_ai = self.original_call_ai
        orchestrator.search_web_once = self.original_search_web_once
        if self.original_automation is None:
            settings.CONFIG.pop("automation", None)
        else:
            settings.CONFIG["automation"] = self.original_automation
        if self.original_third_search is None:
            settings.CONFIG.pop("third_search", None)
        else:
            settings.CONFIG["third_search"] = self.original_third_search
        self.temp_dir.cleanup()

    @staticmethod
    def fake_search_web_once(query):
        return (
            ToolResult(
                "搜索结果",
                1,
                [
                    {
                        "query": query,
                        "title": "测试来源",
                        "url": "https://example.com/source",
                        "snippet": "外部研究提供了不同边界与反例。",
                        "published": "2026-07-14",
                    }
                ],
            ),
            "",
        )

    def fake_call_ai(self, prompt, model_cfg, **kwargs):
        self.ai_calls.append(prompt)
        if "[程序 Agent 任务:" not in prompt:
            return "测试总结", True
        input_text = prompt.split("【中控提供的输入】\n", 1)[1]
        data, _ = json.JSONDecoder().raw_decode(input_text)
        if "任务:retrospective]" in prompt:
            payload = {"text": "本期完成了一次记录与思考。"}
        elif "任务:research_planner]" in prompt:
            payload = {
                "action": "search",
                "query": "记录与反思方法的研究和边界",
            }
        elif "任务:researcher]" in prompt:
            payload = {
                "status": "supported",
                "text": "外部研究提供了不同边界与反例。",
            }
        else:
            payload = {"approved": True, "feedback": ""}
        return AIResponse(
            json.dumps(payload, ensure_ascii=False),
            True,
            {
                "usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 3,
                    "total_tokens": 10,
                    "cached_tokens": 2,
                }
            },
        )

    def write_diary(self, date: str):
        path = settings.DIARY_DIR / f"{date}.md"
        raw = "**09:00:** 我开始重视记录是否可以验证。\n\n"
        path.write_text(
            f"# {date}\n\n<summary>\n旧总结\n</summary>\n\n---\n## 原始记录流\n\n{raw}",
            encoding="utf-8",
        )
        return path

    def test_summary_only_replaces_summary_region(self):
        diary = self.write_diary("2026-07-14")
        _, success = orchestrator.summarize_diary("2026-07-14", {"name": "mock"})
        self.assertTrue(success)
        content = diary.read_text(encoding="utf-8")
        self.assertIn("<summary>\n测试总结\n</summary>", content)
        self.assertIn("我开始重视记录是否可以验证", content)

    def test_weekly_report_has_two_independently_generated_sections(self):
        day = datetime.date(2026, 7, 14)
        diary = self.write_diary(day.isoformat())
        original = diary.read_bytes()
        _, success, path = orchestrator.generate_analysis_report(
            "weekly", day, {"name": "mock"}
        )
        self.assertTrue(success)
        content = path.read_text(encoding="utf-8")
        self.assertIn("## 一、整理与回顾", content)
        self.assertIn("## 二、领域探索与研究", content)
        self.assertIn("### 记录与反思方法", content)
        self.assertIn("https://example.com/source", content)
        self.assertEqual(original, diary.read_bytes())
        retrospective_review = next(
            prompt
            for prompt in self.ai_calls
            if "任务:reviewer]" in prompt and '"mode": "retrospective_review"' in prompt
        )
        self.assertIn("我开始重视记录是否可以验证", retrospective_review)

    def test_retry_reuses_reviewed_stages_from_equivalent_failed_run(self):
        day = datetime.date(2026, 7, 14)
        self.write_diary(day.isoformat())
        with patch.object(
            orchestrator,
            "_research_section",
            side_effect=AgentPipelineError("模拟 Researcher 失败"),
        ):
            _, first_success, _ = orchestrator.generate_analysis_report(
                "weekly", day, {"name": "mock"}
            )
        self.assertFalse(first_success)

        self.ai_calls.clear()
        _, second_success, _ = orchestrator.generate_analysis_report(
            "weekly", day, {"name": "mock"}
        )

        self.assertTrue(second_success)
        self.assertFalse(
            any("任务:retrospective]" in prompt for prompt in self.ai_calls)
        )
        self.assertFalse(
            any("任务:research_planner]" in prompt for prompt in self.ai_calls)
        )
        self.assertTrue(any("任务:researcher]" in prompt for prompt in self.ai_calls))

    def test_daily_analysis_is_removed(self):
        self.write_diary("2026-07-14")
        message, success, path = orchestrator.generate_analysis_report(
            "daily", datetime.date(2026, 7, 14), {"name": "mock"}
        )
        self.assertFalse(success)
        self.assertIsNone(path)
        self.assertIn("weekly", message)

    def test_monthly_report_only_summarizes_without_search(self):
        day = datetime.date(2026, 7, 14)
        self.write_diary(day.isoformat())
        settings.CONFIG["third_search"] = {"enabled": False}

        message, success, path = orchestrator.generate_analysis_report(
            "monthly", day, {"name": "mock"}
        )

        self.assertTrue(success, message)
        content = path.read_text(encoding="utf-8")
        self.assertIn("## 整理与回顾", content)
        self.assertNotIn("领域探索与研究", content)
        self.assertFalse(
            any("任务:research_planner]" in prompt for prompt in self.ai_calls)
        )
        self.assertFalse(any("任务:researcher]" in prompt for prompt in self.ai_calls))

    def test_report_header_shows_model_duration_and_token_usage(self):
        day = datetime.date(2026, 7, 14)
        self.write_diary(day.isoformat())

        with patch.object(
            orchestrator.time, "perf_counter", side_effect=[100.0, 165.0]
        ):
            message, success, path = orchestrator.generate_analysis_report(
                "monthly",
                day,
                {"name": "测试模型", "model_id": "mock-v1"},
            )

        self.assertTrue(success, message)
        content = path.read_text(encoding="utf-8")
        self.assertIn("> 使用模型：mock-v1", content)
        self.assertNotIn("测试模型（", content)
        self.assertIn("> 生成耗时：1 分 5 秒", content)
        self.assertIn(
            "> Token 用量：20（输入 14，输出 6，缓存命中 4）", content
        )

    def test_manual_and_automatic_reports_remain_separate(self):
        day = datetime.date(2026, 7, 14)
        self.write_diary(day.isoformat())
        _, manual, manual_path = orchestrator.generate_analysis_report(
            "weekly", day, {"name": "mock"}, origin="manual"
        )
        _, auto, auto_path = orchestrator.generate_analysis_report(
            "weekly", day, {"name": "mock"}, origin="auto", trigger="retry"
        )
        self.assertTrue(manual and auto)
        self.assertNotEqual(manual_path, auto_path)
        self.assertIn("自动任务重试", auto_path.read_text(encoding="utf-8"))

    def test_report_reference_is_not_loaded_but_diary_reference_is(self):
        day = "2026-07-14"
        report = settings.ANALYSIS_DIR / "Weekly" / "old_auto.md"
        report.parent.mkdir(parents=True)
        report.write_text("不应读取的报告内容", encoding="utf-8")
        older = settings.DIARY_DIR / "2026-07-13.md"
        older.write_text(
            "# 2026-07-13\n\n**08:00:** 可以读取的日记内容\n",
            encoding="utf-8",
        )
        logs = [
            (
                day,
                "**09:00 [引用]:** [旧报告](<../AnalysisReports/Weekly/old_auto.md>)\n\n"
                "**10:00 [引用]:** [日记](<2026-07-13.md>)\n\n",
            )
        ]
        loaded = context._referenced_source_context(logs)
        self.assertIn("可以读取的日记内容", loaded)
        self.assertNotIn("不应读取的报告内容", loaded)

    def test_referenced_diary_records_are_addressable_and_reach_reviewer(self):
        old = settings.DIARY_DIR / "2026-07-13.md"
        old.write_text(
            "# 2026-07-13\n\n**08:00:** 被引用的旧记录\n",
            encoding="utf-8",
        )
        current = settings.DIARY_DIR / "2026-07-14.md"
        current.write_text(
            "# 2026-07-14\n\n"
            "**09:00 [引用]:** [日记](<2026-07-13.md>)\n\n",
            encoding="utf-8",
        )
        default_fake = self.fake_call_ai

        def reference_fake(prompt, model_cfg, **kwargs):
            if "任务:retrospective]" not in prompt:
                return default_fake(prompt, model_cfg, **kwargs)
            self.ai_calls.append(prompt)
            return AIResponse(
                json.dumps({"text": "引用记录中的事实"}, ensure_ascii=False),
                True,
            )

        orchestrator.call_ai = reference_fake
        message, success, _ = orchestrator.generate_analysis_report(
            "monthly", datetime.date(2026, 7, 14), {"name": "mock"}
        )

        self.assertTrue(success, message)
        review_prompt = next(
            prompt
            for prompt in self.ai_calls
            if "任务:reviewer]" in prompt
            and '"mode": "retrospective_review"' in prompt
        )
        self.assertIn("被引用的旧记录", review_prompt)
        self.assertIn("R-20260713-001", review_prompt)

    def test_legacy_information_briefing_does_not_reach_research_planner(self):
        day = datetime.date(2026, 7, 14)
        self.write_diary(day.isoformat())
        info = settings.ANALYSIS_DIR / "Information" / day.isoformat()
        info.parent.mkdir(parents=True)
        info.with_suffix(".md").write_text("综合新闻雷达线索", encoding="utf-8")
        orchestrator.generate_analysis_report("weekly", day, {"name": "mock"})
        planner_prompt = next(
            prompt for prompt in self.ai_calls if "任务:research_planner]" in prompt
        )
        self.assertNotIn("综合新闻雷达线索", planner_prompt)

    def test_kernel_lock_releases_without_deleting_sentinel(self):
        first = automation._acquire_automation_lock()
        self.assertIsNotNone(first)
        self.assertIsNone(automation._acquire_automation_lock())
        first.release()
        second = automation._acquire_automation_lock()
        self.assertIsNotNone(second)
        second.release()
        self.assertTrue((settings.ANALYSIS_DIR / ".automation.lock").exists())

    def test_placeholder_summary_is_detected_from_diary_content(self):
        today = datetime.date(2026, 7, 17)
        yesterday = today - datetime.timedelta(days=1)
        path = settings.DIARY_DIR / f"{yesterday}.md"
        path.write_text(
            f"# {yesterday}\n\n<summary>\n暂无今日总结。\n</summary>\n\n"
            "---\n## 原始记录流\n\n**09:00:** 昨日记录\n",
            encoding="utf-8",
        )
        state = {}

        with patch.object(
            automation, "summarize_diary", return_value=("总结", True)
        ) as summarize:
            automation._run_daily_summaries(today, state, {"name": "mock"})

        summarize.assert_called_once_with(yesterday.isoformat(), {"name": "mock"})

    def test_existing_summary_is_not_overwritten_on_first_automation_run(self):
        today = datetime.date(2026, 7, 17)
        yesterday = today - datetime.timedelta(days=1)
        path = settings.DIARY_DIR / f"{yesterday}.md"
        path.write_text(
            f"# {yesterday}\n\n<summary>\n已有总结。\n</summary>\n\n"
            "---\n## 原始记录流\n\n**09:00:** 昨日记录\n",
            encoding="utf-8",
        )

        with patch.object(automation, "summarize_diary") as summarize:
            automation._run_daily_summaries(today, {}, {"name": "mock"})

        summarize.assert_not_called()

    def test_three_automatic_tasks_include_summary_and_period_reports(self):
        now = datetime.datetime(2026, 7, 17, 9, 0)
        yesterday = settings.DIARY_DIR / "2026-07-16.md"
        yesterday.write_text(
            "# 2026-07-16\n\n<summary>\n暂无今日总结。\n</summary>\n\n"
            "---\n## 原始记录流\n\n**09:00:** 昨日记录\n",
            encoding="utf-8",
        )
        self.write_diary("2026-07-10")
        self.write_diary("2026-06-15")

        self.assertTrue(automation._task_missing("daily_summary", now))
        self.assertTrue(automation._task_missing("weekly_report", now))
        self.assertTrue(automation._task_missing("monthly_report", now))

        yesterday.write_text(
            yesterday.read_text(encoding="utf-8").replace("暂无今日总结。", "已有总结。"),
            encoding="utf-8",
        )
        week_start, week_end = automation._latest_week_period(now.date())
        month_start, month_end = automation._latest_month_period(now.date())
        weekly_path = context._analysis_report_path(
            "weekly", week_start, week_end, "auto"
        )
        monthly_path = context._analysis_report_path(
            "monthly", month_start, month_end, "auto"
        )
        weekly_path.parent.mkdir(parents=True, exist_ok=True)
        monthly_path.parent.mkdir(parents=True, exist_ok=True)
        weekly_path.write_text("周报", encoding="utf-8")
        monthly_path.write_text("月报", encoding="utf-8")

        for task in automation.AUTOMATION_TASK_LABELS:
            self.assertFalse(automation._task_missing(task, now), task)

    def test_latest_auto_reports_are_detected_from_files(self):
        today = datetime.date(2026, 7, 17)
        self.write_diary("2026-07-10")
        self.write_diary("2026-06-15")
        state = {}

        with patch.object(
            automation,
            "generate_analysis_report",
            return_value=("完成", True, Path("auto.md")),
        ) as generate:
            automation._run_weekly_reports(today, state, {"name": "mock"})
            automation._run_monthly_reports(today, state, {"name": "mock"})

        self.assertEqual(
            ["weekly", "monthly"],
            [call.args[0] for call in generate.call_args_list],
        )

    def test_retrospective_plain_text_validation_uses_one_revision(self):
        source_id = "R-20260714-001"
        base_input = {
            "period": {"kind": "weekly", "start": "2026-07-13", "end": "2026-07-19"},
            "records": [{"source_id": source_id, "text": "记录"}],
        }
        store = Mock()

        with patch.object(
            orchestrator,
            "_call_agent",
            return_value=({"text": "正文 https://model.example"}, {}),
        ) as call_agent, patch.object(
            orchestrator, "_review_body"
        ) as review, self.assertRaises(AgentPipelineError):
            orchestrator._retrospective_section(
                base_input,
                {source_id},
                {"name": "mock"},
                store,
                "run-id",
            )

        self.assertEqual(2, call_agent.call_count)
        correction = call_agent.call_args.kwargs["revision_context"]
        self.assertEqual("中控确定性校验", correction["feedback_source"])
        self.assertIn("不得自行输出 URL", correction["problems_to_fix"][0])
        review.assert_not_called()

    def test_retrospective_reviewer_can_request_one_content_revision(self):
        source_id = "R-20260714-001"
        base_input = {
            "period": {
                "kind": "weekly",
                "start": "2026-07-13",
                "end": "2026-07-19",
            },
            "records": [{"source_id": source_id, "text": "用户记录"}],
        }
        store = Mock()

        with patch.object(
            orchestrator,
            "_call_agent",
            side_effect=[
                ({"text": "第一稿判断。"}, {}),
                ({"text": "审查后修订。"}, {}),
            ],
        ) as call_agent, patch.object(
            orchestrator,
            "_review_body",
            side_effect=[
                (False, "内容需要修订", {"approved": False}),
                (True, "", {"approved": True}),
            ],
        ):
            markdown = orchestrator._retrospective_section(
                base_input,
                {source_id},
                {"name": "mock"},
                store,
                "run-id",
            )

        self.assertIn("审查后修订", markdown)
        self.assertEqual(2, call_agent.call_count)
        self.assertEqual(
            2,
            call_agent.call_args.kwargs["revision_context"]["maximum_attempts"],
        )

    def test_agent_revision_limit_is_configurable(self):
        source_id = "R-20260714-001"
        base_input = {
            "period": {
                "kind": "weekly",
                "start": "2026-07-13",
                "end": "2026-07-19",
            },
            "records": [{"source_id": source_id, "text": "用户记录"}],
        }
        store = Mock()

        with patch.dict(
            settings.CONFIG,
            {"retry": {"agent_revision_limit": 2}},
        ), patch.object(
            orchestrator,
            "_call_agent",
            side_effect=[
                ({"text": "第一稿。"}, {}),
                ({"text": "第二稿。"}, {}),
                ({"text": "第三稿。"}, {}),
            ],
        ) as call_agent, patch.object(
            orchestrator,
            "_review_body",
            side_effect=[
                (False, "继续修改", {"approved": False}),
                (False, "仍需修改", {"approved": False}),
                (True, "", {"approved": True}),
            ],
        ):
            markdown = orchestrator._retrospective_section(
                base_input,
                {source_id},
                {"name": "mock"},
                store,
                "run-id",
            )

        self.assertIn("第三稿", markdown)
        self.assertEqual(3, call_agent.call_count)
        self.assertEqual(
            3,
            call_agent.call_args.kwargs["revision_context"]["maximum_attempts"],
        )

    def test_reviewer_feedback_is_returned_to_original_agent(self):
        source_id = "R-20260714-001"
        base_input = {
            "period": {"kind": "weekly", "start": "2026-07-13", "end": "2026-07-19"},
            "records": [{"source_id": source_id, "text": "用户记录"}],
        }
        first = "第一稿判断。"
        revised = "修订后仅保留有依据的判断。"
        store = Mock()

        with patch.object(
            orchestrator,
            "_call_agent",
            side_effect=[({"text": first}, {}), ({"text": revised}, {})],
        ) as call_agent, patch.object(
            orchestrator,
            "_review_body",
            side_effect=[
                (False, "删除无依据判断", {"approved": False}),
                (True, "", {"approved": True}),
            ],
        ):
            markdown = orchestrator._retrospective_section(
                base_input,
                {source_id},
                {"name": "mock"},
                store,
                "run-id",
            )

        self.assertIn("修订后", markdown)
        correction = call_agent.call_args_list[1].kwargs["revision_context"]
        self.assertEqual("Reviewer 实质审查", correction["feedback_source"])
        self.assertEqual(first, correction["rejected_previous_output"])
        self.assertIn("删除无依据判断", correction["problems_to_fix"])

    def test_revision_context_does_not_echo_large_internal_telemetry(self):
        correction = orchestrator._revision_context(
            2,
            {
                "markdown": "原稿",
                "_telemetry": {"search_evidence": [{"snippet": "x" * 10000}]},
            },
            ["链接需修正"],
            source="中控确定性校验",
        )

        self.assertEqual(
            {"markdown": "原稿"}, correction["rejected_previous_output"]
        )

    def test_reviewer_receives_all_controller_bound_topic_evidence(self):
        topic = {
            "topic_id": "Q001",
            "title": "公开主题",
            "query": "公开查询",
            "source_refs": ["R-20260714-001"],
        }
        evidence = [
            {
                "source_id": f"W-Q001-00{index}",
                "topic_id": "Q001",
                "title": f"来源{index}",
                "url": f"https://example.com/{index}",
                "snippet": "支持材料",
                "published": "",
            }
            for index in (1, 2)
        ]
        store = Mock()

        with patch.object(
            orchestrator,
            "_call_agent",
            return_value=({"status": "supported", "text": "研究正文"}, {}),
        ), patch.object(
            orchestrator,
            "_review_body",
            return_value=(True, "", {"approved": True}),
        ) as review:
            result = orchestrator._research_one_topic(
                topic, evidence, {"name": "mock"}, store, "run-id"
            )

        self.assertTrue(result["accepted"])
        self.assertEqual(2, len(review.call_args.args[2]["evidence_sources"]))

    def test_grounded_research_searches_once_and_controller_renders_url(self):
        topics = [
            {
                "topic_id": "Q001",
                "title": "公开主题",
                "query": "公开查询",
                "reason": "研究",
                "origin": "news",
                "source_refs": [],
            }
        ]
        store = Mock()
        result = ToolResult(
            "搜索结果",
            1,
            [
                {
                    "query": "公开查询",
                    "title": "真实来源",
                    "url": "https://example.com/real",
                    "snippet": "支持材料",
                    "published": "2026-07-14",
                }
            ],
        )
        draft = ({"status": "supported", "text": "基于证据可以确认边界。"}, {})

        with patch.object(
            orchestrator, "third_party_search_available", return_value=True
        ), patch.object(
            orchestrator, "search_web_once", return_value=(result, "")
        ) as search, patch.object(
            orchestrator, "_call_agent", return_value=draft
        ) as call_agent, patch.object(
            orchestrator,
            "_review_body",
            return_value=(True, "", {"approved": True}),
        ):
            markdown = orchestrator._research_section(
                topics, set(), {"name": "mock"}, store, "run-id"
            )

        self.assertEqual(1, search.call_count)
        self.assertIn("https://example.com/real", markdown)
        input_data = call_agent.call_args.args[2]
        self.assertNotIn("information_leads", input_data)
        self.assertNotIn("url", input_data["evidence_sources"][0])

    def test_grounded_research_revision_does_not_repeat_search(self):
        topics = [
            {
                "topic_id": "Q001",
                "title": "公开主题",
                "query": "公开查询",
                "reason": "研究",
                "origin": "news",
                "source_refs": [],
            }
        ]
        result = ToolResult(
            "搜索结果",
            1,
            [
                {
                    "query": "公开查询",
                    "title": "真实来源",
                    "url": "https://example.com/real",
                    "snippet": "支持材料",
                    "published": "",
                }
            ],
        )
        drafts = [
            ({"status": "supported", "text": "第一稿正文。"}, {}),
            ({"status": "supported", "text": "修订后正文。"}, {}),
        ]
        store = Mock()

        with patch.object(
            orchestrator, "search_web_once", return_value=(result, "")
        ) as search, patch.object(
            orchestrator, "_call_agent", side_effect=drafts
        ) as call_agent, patch.object(
            orchestrator,
            "_review_body",
            side_effect=[
                (False, "补充适用边界", {"approved": False}),
                (True, "", {"approved": True}),
            ],
        ):
            markdown = orchestrator._grounded_research_section(
                topics, set(), {"name": "mock"}, store, "run-id"
            )

        self.assertIn("https://example.com/real", markdown)
        self.assertEqual(1, search.call_count)
        self.assertEqual(2, call_agent.call_count)

    def test_grounded_research_delivers_accepted_topics_without_rewriting_rejected_one(self):
        topics = [
            {
                "topic_id": f"Q{index:03d}",
                "title": title,
                "query": f"查询{index}",
                "reason": "研究",
                "origin": "news",
                "source_refs": [],
            }
            for index, title in ((1, "可交付主题"), (2, "应丢弃主题"))
        ]
        evidence = [
            {
                "source_id": f"W-Q{index:03d}-001",
                "topic_id": f"Q{index:03d}",
                "query": f"查询{index}",
                "title": f"来源{index}",
                "url": f"https://example.com/{index}",
                "snippet": "摘要",
                "published": "",
            }
            for index in (1, 2)
        ]
        store = Mock()

        with patch.object(
            orchestrator,
            "_collect_research_evidence",
            return_value=(topics, evidence, {"search_evidence": evidence}),
        ), patch.object(
            orchestrator,
            "_research_one_topic",
            side_effect=[
                {
                    "topic": topics[0],
                    "accepted": True,
                    "markdown": "### 可交付主题\n\n正文",
                    "sources": [evidence[0]],
                },
                {
                    "topic": topics[1],
                    "accepted": False,
                    "feedback": "证据不足",
                },
            ],
        ) as research_one:
            markdown = orchestrator._grounded_research_section(
                topics, set(), {"name": "mock"}, store, "run-id"
            )

        self.assertIn("### 可交付主题", markdown)
        self.assertNotIn("### 应丢弃主题", markdown)
        self.assertEqual(2, research_one.call_count)

    def test_grounded_search_cache_avoids_searching_again_on_report_retry(self):
        topics = [
            {
                "topic_id": "Q001",
                "title": "公开主题",
                "query": "公开查询",
                "reason": "研究",
                "origin": "news",
                "source_refs": [],
            }
        ]
        evidence = [
            {
                "source_id": "W-Q001-001",
                "topic_id": "Q001",
                "query": "公开查询",
                "title": "真实来源",
                "url": "https://example.com/real",
                "snippet": "支持材料",
                "published": "",
            }
        ]
        telemetry = {
            "tool_calls": {"web_search": 1},
            "search_queries": ["公开查询"],
            "search_results": 1,
            "search_evidence": evidence,
        }
        cached = (
            "previous-run",
            {
                "topics": topics,
                "usable_topics": topics,
                "evidence": evidence,
                "_telemetry": telemetry,
            },
        )
        store = Mock()

        with patch.object(orchestrator, "search_web_once") as search:
            usable, restored, restored_telemetry = (
                orchestrator._collect_research_evidence(
                    topics, store, "run-id", cached
                )
            )

        search.assert_not_called()
        self.assertEqual(topics, usable)
        self.assertEqual(evidence, restored)
        self.assertEqual(telemetry, restored_telemetry)
        saved = store.save_artifact.call_args.args[2]
        self.assertTrue(saved["_cache"]["hit"])

    def test_grounded_search_cache_requires_safe_evidence_for_every_topic(self):
        topics = [
            {
                "topic_id": "Q001",
                "title": "主题一",
                "query": "查询一",
                "reason": "研究",
                "origin": "news",
                "source_refs": [],
            },
            {
                "topic_id": "Q002",
                "title": "主题二",
                "query": "查询二",
                "reason": "研究",
                "origin": "news",
                "source_refs": [],
            },
        ]
        payload = {
            "topics": topics,
            "usable_topics": topics,
            "evidence": [
                {
                    "source_id": "W-Q001-001",
                    "topic_id": "Q001",
                    "title": "来源",
                    "url": "https://example.com/unsafe\nlink",
                    "snippet": "材料",
                    "published": "",
                }
            ],
            "_telemetry": {},
        }

        self.assertIsNone(
            orchestrator._valid_cached_research_evidence(payload, topics)
        )

    def test_invalid_agent_json_fails_without_a_repair_call(self):
        parse_error = AgentPipelineError(
            "Agent JSON 无法解析: test",
            response="invalid",
            telemetry={
                "web_citations": 0,
                "tool_calls": {"web_search": 1},
                "search_results": 12,
            },
        )
        store = Mock()

        with patch.object(
            orchestrator, "invoke_agent", side_effect=parse_error
        ) as invoke, self.assertRaises(AgentPipelineError):
            orchestrator._call_agent(
                orchestrator.researcher.SPEC,
                "研究",
                {},
                {"name": "mock"},
                store,
                "run-id",
            )

        self.assertEqual(1, invoke.call_count)
        saved_payload = store.save_artifact.call_args.args[2]
        self.assertEqual(1, saved_payload["_telemetry"]["tool_calls"]["web_search"])
        self.assertEqual(12, saved_payload["_telemetry"]["search_results"])

    def test_api_failure_does_not_enter_content_revision_loop(self):
        store = Mock()
        failure = AgentPipelineError(
            "research_planner 调用失败: 网络异常: timeout"
        )

        with patch.object(
            orchestrator, "_call_agent", side_effect=failure
        ) as call_agent, self.assertRaises(AgentPipelineError):
            orchestrator._research_topics(
                {
                    "period": {"kind": "weekly"},
                    "records": [
                        {
                            "date": "2026-07-14",
                            "source_id": "R-20260714-001",
                            "text": "记录",
                        }
                    ],
                },
                {"R-20260714-001"},
                {"name": "mock"},
                store,
                "run-id",
            )

        self.assertEqual(1, call_agent.call_count)

    def test_planner_dispatches_all_seven_days_in_at_most_five_groups(self):
        records = [
            {
                "date": f"2026-07-{day:02d}",
                "source_id": f"R-202607{day:02d}-001",
                "text": f"第 {day} 日记录",
            }
            for day in range(13, 20)
        ]
        responses = [
            ({"action": "search", "query": f"公开研究问题 {index}"}, {})
            for index in range(1, 6)
        ]
        store = Mock()

        with patch.object(
            orchestrator, "_call_agent", side_effect=responses
        ) as call_agent:
            topics = orchestrator._research_topics(
                {"period": {"kind": "weekly"}, "records": records},
                {record["source_id"] for record in records},
                {"name": "mock"},
                store,
                "run-id",
            )

        self.assertEqual(5, call_agent.call_count)
        groups = [call.args[2]["record_group"] for call in call_agent.call_args_list]
        dispatched_ids = [
            record["source_id"]
            for group in groups
            for record in group["records"]
        ]
        self.assertCountEqual(
            [record["source_id"] for record in records], dispatched_ids
        )
        self.assertEqual(len(dispatched_ids), len(set(dispatched_ids)))
        self.assertCountEqual(
            dispatched_ids,
            [source_id for topic in topics for source_id in topic["source_refs"]],
        )

    def test_retry_runs_all_failed_tasks(self):
        settings.ANALYSIS_DIR.mkdir()
        (settings.ANALYSIS_DIR / ".automation-state.json").write_text(
            json.dumps(
                {
                    "errors": {
                        "daily_summary": "失败",
                        "daily_information": "失败",
                        "weekly_report": "失败",
                        "monthly_report": "失败",
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        calls = []

        def succeed(task, now, state, model):
            calls.append(task)
            automation._clear_task_error(state, task)
            automation._save_automation_state(state)

        with patch.object(
            automation, "_automation_model", return_value={"name": "mock"}
        ), patch.object(automation, "_retry_one_task", side_effect=succeed):
            _, success = automation.retry_failed_automatic_tasks()
        self.assertTrue(success)
        self.assertEqual(
            ["daily_summary", "weekly_report", "monthly_report"], calls
        )
        self.assertNotIn(
            "daily_information", automation._load_automation_state().get("errors", {})
        )

    def test_retry_stops_after_any_predecessor_failure(self):
        settings.ANALYSIS_DIR.mkdir()
        automation._save_automation_state(
            {
                "errors": {
                    "daily_summary": "失败",
                    "weekly_report": "失败",
                }
            }
        )
        calls = []

        def still_fails(task, now, state, model):
            calls.append(task)
            automation._set_task_error(state, task, "内容校验失败")
            automation._save_automation_state(state)

        with patch.object(
            automation, "_automation_model", return_value={"name": "mock"}
        ), patch.object(automation, "_retry_one_task", side_effect=still_fails):
            _, success = automation.retry_failed_automatic_tasks()

        self.assertFalse(success)
        self.assertEqual(["daily_summary"], calls)

    def test_scheduler_does_not_cross_predecessor_retry_barrier(self):
        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 7, 17, 9, 0)

        settings.CONFIG["automation"] = {
            "enabled": True,
            "daily_summary": True,
            "weekly_report": True,
            "monthly_report": False,
        }
        settings.ANALYSIS_DIR.mkdir()
        automation._save_automation_state(
            {
                "errors": {"daily_summary": "2026-07-17 08:30 内容失败"},
                "retry_after": {"daily_summary": "2026-07-17T10:00:00"},
                "retry_kind": {"daily_summary": "hourly"},
            }
        )

        with patch.object(
            automation.datetime, "datetime", FixedDateTime
        ), patch.object(
            automation,
            "_task_missing",
            side_effect=lambda task, now: task in {"daily_summary", "weekly_report"},
        ), patch.object(automation, "_run_weekly_reports") as run_weekly:
            automation.run_due_automatic_tasks()

        run_weekly.assert_not_called()

    def test_minute_scheduler_does_not_repeat_recorded_failures(self):
        settings.CONFIG["automation"] = {
            "enabled": True,
            "daily_summary": False,
            "weekly_report": True,
            "monthly_report": True,
        }
        settings.ANALYSIS_DIR.mkdir()
        (settings.ANALYSIS_DIR / ".automation-state.json").write_text(
            json.dumps(
                {
                    "errors": {
                        "weekly_report": "失败",
                        "monthly_report": "失败",
                    }
                }
            ),
            encoding="utf-8",
        )

        with patch.object(
            automation, "_automation_model", return_value={"name": "mock"}
        ), patch.object(automation, "_run_weekly_reports") as weekly, patch.object(
            automation, "_run_monthly_reports"
        ) as monthly:
            automation.run_due_automatic_tasks()

        weekly.assert_not_called()
        monthly.assert_not_called()

    def test_first_minute_after_a_missed_hour_runs_detection(self):
        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 7, 17, 10, 23)

        settings.CONFIG["automation"] = {
            "enabled": True,
            "daily_summary": False,
            "weekly_report": True,
            "monthly_report": False,
        }
        settings.ANALYSIS_DIR.mkdir()
        (settings.ANALYSIS_DIR / ".automation-state.json").write_text(
            json.dumps({"last_detection_hour": "2026-07-17T08"}),
            encoding="utf-8",
        )

        with patch.object(
            automation.datetime, "datetime", FixedDateTime
        ), patch.object(
            automation,
            "_task_missing",
            side_effect=lambda task, now: task == "weekly_report",
        ), patch.object(
            automation, "_automation_model", return_value={"name": "mock"}
        ), patch.object(automation, "_run_weekly_reports") as run_weekly:
            automation.run_due_automatic_tasks()

        run_weekly.assert_called_once()
        state = automation._load_automation_state()
        self.assertEqual("2026-07-17T10", state["last_detection_hour"])

    def test_failed_task_becomes_due_at_the_next_clock_hour(self):
        state = {
            "errors": {"weekly_report": "2026-07-17 00:25 周报失败"},
            "retry_after": {"weekly_report": "2026-07-17T01:00:00"},
        }

        self.assertFalse(
            automation._failure_retry_is_due(
                state, "weekly_report", datetime.datetime(2026, 7, 17, 0, 59)
            )
        )
        self.assertTrue(
            automation._failure_retry_is_due(
                state, "weekly_report", datetime.datetime(2026, 7, 17, 1, 0)
            )
        )

    def test_recorded_failure_sets_and_clears_next_hour_deadline(self):
        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 7, 17, 0, 25, 41)

        state = {}
        with patch.object(
            automation.datetime, "datetime", FixedDateTime
        ), patch.object(
            automation, "_content_failure_key", return_value="same-input"
        ):
            automation._set_task_error(state, "weekly_report", "周报失败")

        self.assertEqual(
            "2026-07-17T01:00:00", state["retry_after"]["weekly_report"]
        )
        self.assertEqual("content", state["retry_kind"]["weekly_report"])
        self.assertEqual(1, state["failure_counts"]["weekly_report"])
        self.assertEqual(
            {"start": "2026-07-06", "end": "2026-07-12"},
            state["failure_targets"]["weekly_report"],
        )
        automation._clear_task_error(state, "weekly_report")
        self.assertNotIn("errors", state)
        self.assertNotIn("retry_after", state)
        self.assertNotIn("retry_kind", state)
        self.assertNotIn("failure_counts", state)
        self.assertNotIn("failure_keys", state)
        self.assertNotIn("failure_targets", state)

    def test_retry_processes_cross_day_followups_with_original_targets(self):
        summary_target = {"start": "2026-07-16", "end": "2026-07-16"}
        weekly_target = {"start": "2026-07-06", "end": "2026-07-12"}
        state = {
            "errors": {"daily_summary": "2026-07-17 09:00 失败"},
            "failure_targets": {"daily_summary": summary_target},
            "pending_targets": {
                "daily_summary": [summary_target],
                "weekly_report": [weekly_target],
            },
        }
        automation._save_automation_state(state)
        calls = []

        def retry_summary(task, now, current_state, model):
            calls.append((task, summary_target))
            automation._clear_task_error(current_state, task)
            automation._save_automation_state(current_state)

        def run_weekly(today, current_state, model, *, trigger, target):
            self.assertEqual("retry", trigger)
            calls.append(("weekly_report", target))

        with patch.object(
            automation, "_retry_one_task", side_effect=retry_summary
        ), patch.object(
            automation, "_task_should_run", return_value=True
        ), patch.object(
            automation, "_run_weekly_reports", side_effect=run_weekly
        ):
            automation._process_pending_targets(
                datetime.datetime(2026, 7, 18, 9, 0),
                state,
                {"name": "mock"},
                manual_retry=True,
                process_all=True,
            )

        self.assertEqual(
            [
                ("daily_summary", summary_target),
                ("weekly_report", weekly_target),
            ],
            calls,
        )
        self.assertNotIn("pending_targets", automation._load_automation_state())

    def test_oversized_retrospective_records_are_processed_in_ordered_chunks(self):
        text = "内容" * 1800
        source_id = "R-20260714-001-123456789abc"
        base_input = {
            "period": {"kind": "weekly", "start": "2026-07-13", "end": "2026-07-19"},
            "records": [{"source_id": source_id, "text": text}],
            "referenced_records": [],
        }
        seen_parts = []

        def process_chunk(chunk_input, *args, **kwargs):
            seen_parts.extend(record["text"] for record in chunk_input["records"])
            return f"分块 {chunk_input['chunk']['index']} [{source_id}]"

        store = Mock()
        with patch.object(
            orchestrator, "_MAX_AGENT_INPUT_CHARACTERS", 1000
        ), patch.object(
            orchestrator, "_MAX_RECORD_CHUNK_CHARACTERS", 1000
        ), patch.object(
            orchestrator, "_retrospective_section", side_effect=process_chunk
        ) as section:
            markdown = orchestrator._retrospective_with_input_budget(
                base_input,
                {source_id},
                {"name": "mock"},
                store,
                "run-id",
            )

        self.assertGreater(section.call_count, 1)
        self.assertEqual(text, "".join(seen_parts))
        self.assertIn("分块 1", markdown)

    def test_same_content_failure_stops_after_one_automatic_retry(self):
        state = {}
        with patch.object(
            automation, "_content_failure_key", return_value="same-input"
        ):
            automation._set_task_error(state, "weekly_report", "第一次失败")
            automation._set_task_error(state, "weekly_report", "第二次失败")

        self.assertEqual("content_blocked", state["retry_kind"]["weekly_report"])
        self.assertEqual(2, state["failure_counts"]["weekly_report"])
        self.assertNotIn("weekly_report", state.get("retry_after", {}))
        self.assertFalse(
            automation._failure_retry_is_due(
                state, "weekly_report", datetime.datetime(2026, 7, 17, 12, 0)
            )
        )

    def test_automatic_failure_limit_and_retry_boundary_are_configurable(self):
        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 7, 17, 0, 25, 41)

        state = {}
        with patch.dict(
            settings.CONFIG,
            {
                "retry": {
                    "automation_content_failure_limit": 3,
                    "automation_content_retry_interval_minutes": 30,
                }
            },
        ), patch.object(
            automation, "_content_failure_key", return_value="same-input"
        ), patch.object(automation.datetime, "datetime", FixedDateTime):
            automation._set_task_error(state, "weekly_report", "第一次失败")
            self.assertEqual(
                "2026-07-17T00:30:00",
                state["retry_after"]["weekly_report"],
            )
            automation._set_task_error(state, "weekly_report", "第二次失败")
            self.assertEqual("content", state["retry_kind"]["weekly_report"])
            automation._set_task_error(state, "weekly_report", "第三次失败")

        self.assertEqual("content_blocked", state["retry_kind"]["weekly_report"])
        self.assertEqual(3, state["failure_counts"]["weekly_report"])
        self.assertNotIn("weekly_report", state.get("retry_after", {}))

    def test_changed_input_unlocks_content_failure_on_hourly_detection(self):
        state = {
            "errors": {"weekly_report": "连续失败"},
            "retry_kind": {"weekly_report": "content_blocked"},
            "failure_counts": {"weekly_report": 2},
            "failure_keys": {"weekly_report": "old-input"},
        }
        now = datetime.datetime(2026, 7, 17, 12, 0)
        with patch.object(
            automation, "_content_failure_key", return_value="new-input"
        ), patch.object(automation, "_task_missing", return_value=True):
            should_run = automation._task_should_run(
                state,
                "weekly_report",
                now,
                initial_detection_due=True,
            )

        self.assertTrue(should_run)
        self.assertNotIn("errors", state)
        self.assertNotIn("failure_counts", state)

    def test_network_failure_sets_five_minute_retry_deadline(self):
        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 7, 17, 0, 25, 41)

        state = {}
        with patch.object(automation.datetime, "datetime", FixedDateTime):
            automation._set_task_error(
                state, "weekly_report", "网络异常: DNS 解析失败"
            )

        self.assertEqual(
            "2026-07-17T00:30:41",
            state["retry_after"]["weekly_report"],
        )
        self.assertEqual("network", state["retry_kind"]["weekly_report"])

    def test_auth_failure_waits_for_manual_retry_after_configuration_fix(self):
        state = {}
        automation._set_task_error(
            state, "weekly_report", "配置异常: HTTP 401"
        )

        self.assertEqual("blocked", state["retry_kind"]["weekly_report"])
        self.assertNotIn("weekly_report", state.get("retry_after", {}))
        self.assertFalse(
            automation._failure_retry_is_due(
                state, "weekly_report", datetime.datetime(2026, 7, 17, 12, 0)
            )
        )

    def test_retry_stops_after_global_provider_failure(self):
        settings.ANALYSIS_DIR.mkdir()
        automation._save_automation_state(
            {
                "errors": {
                    "daily_information": "失败",
                    "weekly_report": "失败",
                    "monthly_report": "失败",
                }
            }
        )
        calls = []

        def fail_network(task, now, state, model):
            calls.append(task)
            automation._set_task_error(state, task, "网络异常: DNS")
            automation._save_automation_state(state)

        with patch.object(
            automation, "_automation_model", return_value={"name": "mock"}
        ), patch.object(automation, "_retry_one_task", side_effect=fail_network):
            _, success = automation.retry_failed_automatic_tasks()

        self.assertFalse(success)
        self.assertEqual(["weekly_report"], calls)

    def test_monthly_context_excludes_cross_month_weeks_and_deduplicates_origin(self):
        weekly = settings.ANALYSIS_DIR / "Weekly"
        weekly.mkdir(parents=True)
        (weekly / "2026-06-29_to_2026-07-05_auto.md").write_text(
            "跨月内容", encoding="utf-8"
        )
        (weekly / "2026-07-06_to_2026-07-12_auto.md").write_text(
            "自动版", encoding="utf-8"
        )
        (weekly / "2026-07-06_to_2026-07-12_manual.md").write_text(
            "手动版", encoding="utf-8"
        )

        value = context._monthly_supporting_reports(
            datetime.date(2026, 7, 1), datetime.date(2026, 7, 31)
        )

        self.assertNotIn("跨月内容", value)
        self.assertNotIn("自动版", value)
        self.assertIn("手动版", value)

    def test_legacy_completion_cursors_are_removed(self):
        state = {
            "last_daily_date": "2026-07-16",
            "last_information_date": "2026-07-17",
            "last_week_end": "2026-07-12",
            "last_month_end": "2026-06-30",
            "last_deferred_at": "old",
            "deferred_reason": "old",
            "errors": {"weekly_report": "失败"},
        }

        automation._remove_legacy_progress(state)

        self.assertEqual({"errors": {"weekly_report": "失败"}}, state)

    def test_non_object_automation_state_is_ignored(self):
        settings.ANALYSIS_DIR.mkdir()
        (settings.ANALYSIS_DIR / ".automation-state.json").write_text(
            "[]", encoding="utf-8"
        )

        self.assertEqual({}, automation._load_automation_state())

    def test_analysis_cache_signature_tracks_only_effective_dependencies(self):
        first = orchestrator._analysis_config_signature(
            {"name": "mock", "model_id": "v1", "temperature": 0.2},
            "weekly",
        )
        second = orchestrator._analysis_config_signature(
            {"name": "mock", "model_id": "v2", "temperature": 0.8},
            "weekly",
        )

        self.assertNotEqual(first, second)
        self.assertNotIn("api_key", json.dumps(first))

        monthly = orchestrator._analysis_config_signature(
            {"name": "mock", "model_id": "v1"}, "monthly"
        )
        self.assertNotIn("third_search", monthly)

    def test_retry_command_launches_detached_process(self):
        settings.ANALYSIS_DIR.mkdir()
        (settings.ANALYSIS_DIR / ".automation-state.json").write_text(
            json.dumps({"errors": {"weekly_report": "失败"}}), encoding="utf-8"
        )
        with patch.object(automation.subprocess, "Popen") as popen:
            started, _ = automation.launch_automation_retry()
        self.assertTrue(started)
        self.assertIn("--retry-automation", popen.call_args.args[0])
        if automation.os.name != "nt":
            self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_cron_install_checks_every_minute(self):
        listed = Mock(returncode=0, stdout="", stderr="")
        installed = Mock(returncode=0, stdout="", stderr="")
        run = Mock(side_effect=[listed, installed])
        with patch.object(automation, "_is_windows", return_value=False), patch.object(
            automation.subprocess, "run", run
        ):
            success, _ = automation.install_system_automation()
        self.assertTrue(success)
        cron_input = run.call_args_list[1].kwargs["input"]
        self.assertIn("@reboot", cron_input)
        self.assertIn("* * * * *", cron_input)
        self.assertIn(" minute", cron_input)

    def test_windows_frozen_automation_uses_windowless_companion(self):
        root = Path(self.temp_dir.name)
        foreground = root / "AgentRecord.exe"
        background = root / "AgentRecordBackground.exe"
        foreground.touch()
        background.touch()

        with patch.object(automation, "_is_windows", return_value=True), patch.object(
            automation.sys, "executable", str(foreground)
        ), patch.object(automation.sys, "frozen", True, create=True):
            command = automation._automation_command()

        self.assertEqual(str(background), command[0])

    def test_windows_status_rejects_old_windowed_task_action(self):
        result = Mock(
            returncode=0,
            stdout="<Task><Actions><Exec><Command>C:\\AgentRecord.exe</Command>"
            "</Exec></Actions></Task>",
            stderr="",
        )
        with patch.object(automation, "_is_windows", return_value=True), patch.object(
            automation.subprocess, "run", return_value=result
        ):
            installed, message = automation.system_automation_status()

        self.assertFalse(installed)
        self.assertIn("旧入口", message)

    def test_windows_status_requires_exact_executable_and_arguments(self):
        expected = [r"C:\New\AgentRecordBackground.exe", "--run-automation"]

        def task_xml(command, arguments):
            return (
                '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
                f"<Actions><Exec><Command>{command}</Command>"
                f"<Arguments>{arguments}</Arguments></Exec></Actions></Task>"
            )

        stale = Mock(
            returncode=0,
            stdout=task_xml(
                r"C:\Old\AgentRecordBackground.exe", "--run-automation"
            ),
            stderr="",
        )
        current = Mock(
            returncode=0,
            stdout=task_xml(expected[0], "--run-automation"),
            stderr="",
        )
        with patch.object(automation, "_is_windows", return_value=True), patch.object(
            automation, "_automation_command", return_value=expected
        ), patch.object(
            automation.subprocess, "run", side_effect=[stale, stale]
        ):
            self.assertFalse(automation.system_automation_status()[0])

        with patch.object(automation, "_is_windows", return_value=True), patch.object(
            automation, "_automation_command", return_value=expected
        ), patch.object(
            automation.subprocess, "run", side_effect=[current, current]
        ):
            self.assertTrue(automation.system_automation_status()[0])

    def test_cron_status_rejects_marker_lines_for_an_old_command(self):
        stale = Mock(
            returncode=0,
            stdout=(
                "@reboot /old/agent --run-automation # AgentRecord automation startup\n"
                "* * * * * /old/agent --run-automation # AgentRecord automation minute\n"
            ),
            stderr="",
        )
        with patch.object(automation, "_is_windows", return_value=False), patch.object(
            automation, "_automation_command", return_value=["/new/agent", "--run-automation"]
        ), patch.object(automation.subprocess, "run", return_value=stale):
            installed, _ = automation.system_automation_status()

        self.assertFalse(installed)


if __name__ == "__main__":
    unittest.main()
