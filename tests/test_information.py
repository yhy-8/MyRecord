import datetime
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from AgentRecord import settings
from AgentRecord.ai_client import AIResponse, ToolResult
from AgentRecord.analysis import context, information


class InformationBriefingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.original_diary_dir = settings.DIARY_DIR
        self.original_analysis_dir = settings.ANALYSIS_DIR
        self.original_third_search = settings.CONFIG.get("third_search")
        settings.DIARY_DIR = root / "Records"
        settings.ANALYSIS_DIR = root / "AnalysisReports"
        settings.DIARY_DIR.mkdir()
        settings.CONFIG["third_search"] = {
            "enabled": True,
            "api_key": "test-key",
            "api_url": "https://search.example.test",
        }
        self.model = {
            "name": "mock",
            "search": False,
            "max_tokens": 32768,
        }
        self.search_patch = patch.object(
            information,
            "search_web_once",
            side_effect=self._search_result,
        )
        self.search_mock = self.search_patch.start()

    def tearDown(self):
        self.search_patch.stop()
        settings.DIARY_DIR = self.original_diary_dir
        settings.ANALYSIS_DIR = self.original_analysis_dir
        if self.original_third_search is None:
            settings.CONFIG.pop("third_search", None)
        else:
            settings.CONFIG["third_search"] = self.original_third_search
        self.temp_dir.cleanup()

    @staticmethod
    def _search_result(query: str) -> tuple[ToolResult, str]:
        if "全球 已公布" in query:
            prefix = "global"
        elif "中国 已发布" in query:
            prefix = "china"
        else:
            prefix = "target"
        evidence = [
            {
                "title": f"{prefix} 来源 {number}",
                "url": f"https://example.com/{prefix}-{number}",
                "snippet": f"{prefix} 已公布具体数据 {number * 10}",
                "published": "2026-07-15",
            }
            for number in range(1, 4)
        ]
        return (
            ToolResult(
                "RAW-SEARCH-PROSE-SHOULD-NOT-REACH-COLLECTOR",
                len(evidence),
                evidence,
            ),
            "",
        )

    @staticmethod
    def _response(text: str, success: bool = True) -> AIResponse:
        return AIResponse(
            text,
            success,
            0,
            {},
            0,
            telemetry={
                "http_attempts": 1,
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "cached_tokens": 40,
                    "cache_miss_tokens": 60,
                },
            },
        )

    @staticmethod
    def _prompt_json(prompt: str, heading: str, next_heading: str | None = None):
        value = prompt.split(heading, 1)[1].lstrip()
        if next_heading:
            value = value.split(next_heading, 1)[0]
        payload, _ = json.JSONDecoder().raw_decode(value.strip())
        return payload

    @classmethod
    def _collector_payload(
        cls,
        prompt: str,
        *,
        highlight_count: int = 2,
        targeted_status: str = "supported",
    ) -> dict:
        topics = cls._prompt_json(
            prompt,
            "【本周记录产生的定向选题】",
            "【本轮标准化证据；只能通过 source_id 引用】",
        )
        evidence = cls._prompt_json(
            prompt,
            "【本轮标准化证据；只能通过 source_id 引用】",
        )
        general = [item for item in evidence if item["kind"] == "general"]
        highlights = []
        for number, source in enumerate(general[:highlight_count], 1):
            highlights.append(
                {
                    "title": f"具体变化 {number}",
                    "change": f"机构 {number} 已发布正式结果",
                    "details": [
                        f"数据为 {number * 10}",
                        f"生效日期为 2026-07-{number + 10:02d}",
                    ],
                    "why": f"这会改变对象 {number} 的当前判断依据",
                    "new_since_prior": (
                        "同一来源新增了正式结果"
                        if source["previously_used"]
                        else ""
                    ),
                    "evidence_ids": [source["source_id"]],
                }
            )
        explorations = []
        for topic in topics:
            topic_evidence = [
                item
                for item in evidence
                if item["topic_id"] == topic["topic_id"]
            ]
            if targeted_status == "insufficient_evidence":
                explorations.append(
                    {
                        "status": "insufficient_evidence",
                        "reason": "搜索结果只谈相邻主题，不能回答记录中的问题",
                    }
                )
            else:
                explorations.append(
                    {
                        "status": "supported",
                        "finding": "公开资料给出了可核查的明确结果",
                        "details": ["样本为 100", "结果在 2026-07-15 发布"],
                        "connection": "这些结果直接回应了本周记录提出的判断",
                        "evidence_ids": [topic_evidence[0]["source_id"]],
                    }
                )
        return {
            "highlights": highlights,
            "explorations": explorations,
            "followups": [],
        }

    @staticmethod
    def _planner_payload(prompt: str, query: str = "本地优先软件迁移风险") -> dict:
        record_ref = information._RECORD_REF_PATTERN.findall(prompt)[0]
        return {
            "queries": [
                {
                    "title": "本地优先软件的数据迁移风险",
                    "query": query,
                    "reason": "本周记录正在比较本地优先软件",
                    "record_refs": [record_ref],
                }
            ]
        }

    def _default_model_call(self, prompt, model_config, **_kwargs):
        if prompt.startswith("[程序每日信息选题任务]"):
            return self._response(
                json.dumps(self._planner_payload(prompt), ensure_ascii=False)
            )
        return self._response(
            json.dumps(self._collector_payload(prompt), ensure_ascii=False)
        )

    def _write_record(self, date: str = "2026-07-15") -> None:
        (settings.DIARY_DIR / f"{date}.md").write_text(
            f"# {date}\n\n<summary>\n\n</summary>\n\n"
            "---\n## 原始记录流\n\n"
            "**09:00:** 继续考虑个人知识管理和本地优先软件。\n",
            encoding="utf-8",
        )

    def test_briefing_uses_two_structured_calls_and_controller_renders_sources(self):
        self._write_record()
        prompts = []
        configs = []

        def call(prompt, model_config, **kwargs):
            prompts.append(prompt)
            configs.append(model_config)
            self.assertTrue(kwargs["structured_output"])
            self.assertEqual((), kwargs["allowed_tools"])
            return self._default_model_call(prompt, model_config, **kwargs)

        with patch.object(information, "call_ai", side_effect=call) as model_call:
            body, success, path = information.generate_information_briefing(
                datetime.date(2026, 7, 15),
                self.model,
            )

        self.assertTrue(success)
        self.assertEqual(2, model_call.call_count)
        self.assertEqual(
            [information._PLANNER_MAX_TOKENS, information._COLLECTOR_MAX_TOKENS],
            [config["max_tokens"] for config in configs],
        )
        self.assertNotIn("https://", prompts[1])
        self.assertIn("I-Q001-001", prompts[1])
        self.assertIn("[global 来源 1](https://example.com/global-1)", body)
        self.assertIn("本周记录依据：[R-", body)
        saved = path.read_text(encoding="utf-8")
        self.assertIn("agentrecord-information-index", saved)
        self.assertIn("分析运行：", saved)

    def test_planner_never_receives_old_briefing_or_followup(self):
        self._write_record("2026-07-16")
        directory = settings.ANALYSIS_DIR / "Information"
        directory.mkdir(parents=True)
        (directory / "2026-07-15.md").write_text(
            "# 旧简报\n\n"
            '<!-- agentrecord-targeted-queries: [{"query":"个人知识管理 方法研究",'
            '"reason":"旧记录"}] -->\n\n'
            "## 今日值得关注\n\n### 1. 已覆盖的具体事项\n\n内容。\n\n"
            "## 可继续追踪\n\n欧洲央行决议结果，明天继续搜索。\n",
            encoding="utf-8",
        )
        planner_prompts = []

        def call(prompt, model_config, **kwargs):
            if prompt.startswith("[程序每日信息选题任务]"):
                planner_prompts.append(prompt)
            return self._default_model_call(prompt, model_config, **kwargs)

        with patch.object(information, "call_ai", side_effect=call):
            _, success, _ = information.generate_information_briefing(
                datetime.date(2026, 7, 16),
                self.model,
            )

        self.assertTrue(success)
        self.assertEqual(1, len(planner_prompts))
        self.assertNotIn("欧洲央行", planner_prompts[0])
        self.assertNotIn("已覆盖的具体事项", planner_prompts[0])
        self.assertNotIn("个人知识管理 方法研究", planner_prompts[0])

    def test_controller_deduplicates_old_query_without_exposing_it_to_planner(self):
        self._write_record("2026-07-16")
        directory = settings.ANALYSIS_DIR / "Information"
        directory.mkdir(parents=True)
        (directory / "2026-07-15.md").write_text(
            '<!-- agentrecord-targeted-queries: '
            '[{"query":"个人知识管理 方法研究","reason":"旧记录"}] -->\n',
            encoding="utf-8",
        )
        collector_prompts = []

        def call(prompt, _model_config, **_kwargs):
            if prompt.startswith("[程序每日信息选题任务]"):
                record_ref = information._RECORD_REF_PATTERN.findall(prompt)[0]
                return self._response(
                    json.dumps(
                        {
                            "queries": [
                                {
                                    "title": "重复问题",
                                    "query": "个人知识管理最新进展",
                                    "reason": "重复",
                                    "record_refs": [record_ref],
                                },
                                {
                                    "title": "新的迁移问题",
                                    "query": "本地优先笔记软件数据迁移风险",
                                    "reason": "新角度",
                                    "record_refs": [record_ref],
                                },
                            ]
                        },
                        ensure_ascii=False,
                    )
                )
            collector_prompts.append(prompt)
            return self._response(
                json.dumps(self._collector_payload(prompt), ensure_ascii=False)
            )

        with patch.object(information, "call_ai", side_effect=call):
            _, success, path = information.generate_information_briefing(
                datetime.date(2026, 7, 16),
                self.model,
            )

        self.assertTrue(success)
        self.assertNotIn("重复问题", collector_prompts[0])
        self.assertIn("新的迁移问题", collector_prompts[0])
        self.assertNotIn(
            "个人知识管理最新进展",
            path.read_text(encoding="utf-8"),
        )

    def test_invalid_planner_spends_one_repair_then_degrades_to_general_news(self):
        self._write_record()
        calls = []

        def call(prompt, _model_config, **_kwargs):
            calls.append(prompt)
            if len(calls) <= 2:
                return self._response("不是 JSON")
            return self._response(
                json.dumps(self._collector_payload(prompt), ensure_ascii=False)
            )

        with patch.object(information, "call_ai", side_effect=call):
            _, success, path = information.generate_information_briefing(
                datetime.date(2026, 7, 15),
                self.model,
            )

        self.assertTrue(success)
        self.assertEqual(3, len(calls))
        self.assertTrue(calls[1].startswith(calls[0]))
        self.assertIn("唯一一次结构修订", calls[1])
        self.assertIn("选题 0 项", path.read_text(encoding="utf-8"))

    def test_only_one_repair_is_available_across_both_model_stages(self):
        self._write_record()
        calls = []

        def call(prompt, _model_config, **_kwargs):
            calls.append(prompt)
            if len(calls) == 1:
                return self._response("不是 JSON")
            if len(calls) == 2:
                return self._response(
                    json.dumps(self._planner_payload(prompt), ensure_ascii=False)
                )
            return self._response("Collector 也不是 JSON")

        with patch.object(information, "call_ai", side_effect=call):
            message, success, path = information.generate_information_briefing(
                datetime.date(2026, 7, 15),
                self.model,
            )

        self.assertFalse(success)
        self.assertIsNone(path)
        self.assertEqual(3, len(calls))
        self.assertIn("collector 输出结构无效", message)

    def test_collector_can_use_the_single_repair(self):
        calls = []

        def call(prompt, _model_config, **_kwargs):
            calls.append(prompt)
            if len(calls) == 1:
                return self._response("不是 JSON")
            return self._response(
                json.dumps(self._collector_payload(prompt), ensure_ascii=False)
            )

        with patch.object(information, "call_ai", side_effect=call):
            _, success, _ = information.generate_information_briefing(
                datetime.date(2026, 7, 15),
                self.model,
            )

        self.assertTrue(success)
        self.assertEqual(2, len(calls))
        self.assertTrue(calls[1].startswith(calls[0]))

    def test_highlights_allow_fewer_than_five_and_zero(self):
        evidence = [
            {
                "source_id": "I-Q001-001",
                "kind": "general",
                "topic_id": "",
                "previously_used": False,
            }
        ]
        two = {
            "highlights": [
                {
                    "title": f"事项 {number}",
                    "change": "已经发布结果",
                    "details": ["数据 10"],
                    "why": "改变当前判断",
                    "evidence_ids": ["I-Q001-001"],
                }
                for number in range(2)
            ],
            "explorations": [],
            "followups": [],
        }
        empty = {"highlights": [], "explorations": [], "followups": []}

        self.assertEqual(
            2,
            len(information._normalize_collector_payload(two, evidence, [])["highlights"]),
        )
        self.assertEqual(
            [],
            information._normalize_collector_payload(empty, evidence, [])["highlights"],
        )

    def test_more_than_five_highlights_are_deterministically_truncated(self):
        evidence = [
            {
                "source_id": "I-Q001-001",
                "kind": "general",
                "topic_id": "",
                "previously_used": False,
            }
        ]
        payload = {
            "highlights": [
                {
                    "title": f"事项 {number}",
                    "change": "已经发布结果",
                    "details": ["数据 10"],
                    "why": "改变当前判断",
                    "evidence_ids": ["I-Q001-001"],
                }
                for number in range(6)
            ],
            "explorations": [],
            "followups": [],
        }

        normalized = information._normalize_collector_payload(payload, evidence, [])

        self.assertEqual(5, len(normalized["highlights"]))

    def test_highlight_cannot_use_targeted_evidence_or_raw_url(self):
        evidence = [
            {
                "source_id": "I-Q003-001",
                "kind": "targeted",
                "topic_id": "T001",
                "previously_used": False,
            }
        ]
        payload = {
            "highlights": [
                {
                    "title": "事项",
                    "change": "参见 https://example.com",
                    "details": ["数据 10"],
                    "why": "改变判断",
                    "evidence_ids": ["I-Q003-001"],
                }
            ],
            "explorations": [],
            "followups": [],
        }

        with self.assertRaisesRegex(ValueError, "证据 ID|URL"):
            information._normalize_collector_payload(payload, evidence, [])

    def test_targeted_generic_results_can_be_marked_insufficient_and_are_not_rendered(self):
        self._write_record()

        def call(prompt, model_config, **kwargs):
            if prompt.startswith("[程序每日信息选题任务]"):
                return self._default_model_call(prompt, model_config, **kwargs)
            payload = self._collector_payload(
                prompt,
                targeted_status="insufficient_evidence",
            )
            return self._response(json.dumps(payload, ensure_ascii=False))

        with patch.object(information, "call_ai", side_effect=call):
            body, success, path = information.generate_information_briefing(
                datetime.date(2026, 7, 15),
                self.model,
            )

        self.assertTrue(success)
        self.assertNotIn("### T001.", body)
        self.assertIn("没有证据充分", body)
        self.assertIn(
            "证据不足或不相关跳过 1 项",
            path.read_text(encoding="utf-8"),
        )

    def test_collector_receives_compact_evidence_not_raw_search_prose_or_urls(self):
        directory = settings.ANALYSIS_DIR / "Information"
        directory.mkdir(parents=True)
        (directory / "2026-07-14.md").write_text(
            '<!-- agentrecord-information-index: '
            '{"version":2,"coverage":[{"kind":"highlight",'
            '"title":"昨日事项","source_urls":'
            '["https://example.com/global-1"]}]} -->\n',
            encoding="utf-8",
        )
        collector_prompts = []

        def call(prompt, _model_config, **_kwargs):
            collector_prompts.append(prompt)
            return self._response(
                json.dumps(self._collector_payload(prompt), ensure_ascii=False)
            )

        with patch.object(information, "call_ai", side_effect=call):
            _, success, _ = information.generate_information_briefing(
                datetime.date(2026, 7, 15),
                self.model,
            )

        self.assertTrue(success)
        self.assertEqual(1, len(collector_prompts))
        self.assertNotIn("RAW-SEARCH-PROSE", collector_prompts[0])
        self.assertNotIn("https://", collector_prompts[0])
        self.assertIn("global 已公布具体数据", collector_prompts[0])

    def test_partial_search_failure_keeps_successful_evidence(self):
        def search(query):
            if "全球 已公布" in query:
                return ToolResult(""), "网络异常: 临时 DNS 失败"
            return self._search_result(query)

        with patch.object(
            information,
            "search_web_once",
            side_effect=search,
        ), patch.object(
            information,
            "call_ai",
            side_effect=self._default_model_call,
        ):
            _, success, path = information.generate_information_briefing(
                datetime.date(2026, 7, 15),
                self.model,
            )

        self.assertTrue(success)
        self.assertIn("部分失败 1 项", path.read_text(encoding="utf-8"))

    def test_all_search_failures_preserve_network_classification(self):
        with patch.object(
            information,
            "search_web_once",
            return_value=(ToolResult(""), "网络异常: DNS 失败"),
        ), patch.object(information, "call_ai") as model_call:
            message, success, path = information.generate_information_briefing(
                datetime.date(2026, 7, 15),
                self.model,
            )

        self.assertFalse(success)
        self.assertIsNone(path)
        self.assertIn("网络异常:", message)
        model_call.assert_not_called()

    def test_network_retry_does_not_reuse_failed_empty_search_artifact(self):
        search_calls = []

        def search(query):
            search_calls.append(query)
            if len(search_calls) <= 2:
                return ToolResult(""), "网络异常: DNS 失败"
            return self._search_result(query)

        with patch.object(
            information,
            "search_web_once",
            side_effect=search,
        ), patch.object(
            information,
            "call_ai",
            side_effect=self._default_model_call,
        ):
            _, first_success, _ = information.generate_information_briefing(
                datetime.date(2026, 7, 15),
                self.model,
            )
            _, second_success, _ = information.generate_information_briefing(
                datetime.date(2026, 7, 15),
                self.model,
                trigger="retry",
            )

        self.assertFalse(first_success)
        self.assertTrue(second_success)
        self.assertEqual(4, len(search_calls))

    def test_no_evidence_stops_before_collector(self):
        with patch.object(
            information,
            "search_web_once",
            return_value=(ToolResult("搜索无结果"), ""),
        ), patch.object(information, "call_ai") as model_call:
            message, success, path = information.generate_information_briefing(
                datetime.date(2026, 7, 15),
                self.model,
            )

        self.assertFalse(success)
        self.assertIsNone(path)
        self.assertIn("没有返回可审计的来源证据", message)
        model_call.assert_not_called()

    def test_failed_collector_retry_reuses_planner_and_search_artifacts(self):
        self._write_record()
        responses = []

        def call(prompt, model_config, **kwargs):
            if prompt.startswith("[程序每日信息选题任务]"):
                responses.append("planner")
                return self._default_model_call(prompt, model_config, **kwargs)
            collector_count = sum(value == "collector" for value in responses)
            responses.append("collector")
            if collector_count < 2:
                return self._response("不是 JSON")
            return self._response(
                json.dumps(self._collector_payload(prompt), ensure_ascii=False)
            )

        with patch.object(information, "call_ai", side_effect=call):
            _, first_success, _ = information.generate_information_briefing(
                datetime.date(2026, 7, 15),
                self.model,
            )
            first_search_count = self.search_mock.call_count
            _, second_success, path = information.generate_information_briefing(
                datetime.date(2026, 7, 15),
                self.model,
                trigger="retry",
            )

        self.assertFalse(first_success)
        self.assertTrue(second_success)
        self.assertTrue(path.exists())
        self.assertEqual(3, first_search_count)
        self.assertEqual(first_search_count, self.search_mock.call_count)
        self.assertEqual(["planner", "collector", "collector", "collector"], responses)

    def test_completed_run_and_stage_telemetry_are_auditable(self):
        with patch.object(
            information,
            "call_ai",
            side_effect=self._default_model_call,
        ):
            _, success, path = information.generate_information_briefing(
                datetime.date(2026, 7, 15),
                self.model,
            )

        self.assertTrue(success)
        database = settings.ANALYSIS_DIR / ".analysis.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            run = connection.execute(
                "SELECT kind, status, report_path FROM analysis_runs"
            ).fetchone()
            artifact_json = connection.execute(
                """
                SELECT payload_json FROM agent_artifacts
                WHERE agent = 'daily_information_collector'
                """
            ).fetchone()[0]
        payload = json.loads(artifact_json)
        self.assertEqual(("daily_information", "completed", str(path)), run)
        self.assertEqual(120, payload["_telemetry"]["usage"]["total_tokens"])
        self.assertEqual(40, payload["_telemetry"]["usage"]["cached_tokens"])

    def test_previous_briefing_index_excludes_followups_target_day_and_old_week(self):
        directory = settings.ANALYSIS_DIR / "Information"
        directory.mkdir(parents=True)
        (directory / "2026-07-12.md").write_text(
            "## 今日值得关注\n\n### 1. 上周事项\n",
            encoding="utf-8",
        )
        (directory / "2026-07-15.md").write_text(
            "## 今日值得关注\n\n### 1. 本周已覆盖事项\n\n正文\n\n"
            "## 可继续追踪\n\n绝不能进入索引的明日问题\n",
            encoding="utf-8",
        )
        (directory / "2026-07-16.md").write_text(
            "## 今日值得关注\n\n### 1. 当天旧简报\n",
            encoding="utf-8",
        )

        index_text, _ = information._prior_week_briefings(
            datetime.date(2026, 7, 16)
        )

        self.assertIn("本周已覆盖事项", index_text)
        self.assertNotIn("绝不能进入索引", index_text)
        self.assertNotIn("上周事项", index_text)
        self.assertNotIn("当天旧简报", index_text)

    def test_zero_evidence_target_is_removed_before_collector(self):
        self._write_record()

        def search(query):
            if "全球 已公布" in query or "中国 已发布" in query:
                return self._search_result(query)
            return ToolResult("搜索无结果"), ""

        collector_prompts = []

        def call(prompt, model_config, **kwargs):
            if prompt.startswith("[程序每日信息选题任务]"):
                return self._default_model_call(prompt, model_config, **kwargs)
            collector_prompts.append(prompt)
            return self._response(
                json.dumps(self._collector_payload(prompt), ensure_ascii=False)
            )

        with patch.object(
            information,
            "search_web_once",
            side_effect=search,
        ), patch.object(information, "call_ai", side_effect=call):
            _, success, path = information.generate_information_briefing(
                datetime.date(2026, 7, 15),
                self.model,
            )

        self.assertTrue(success)
        topics = self._prompt_json(
            collector_prompts[0],
            "【本周记录产生的定向选题】",
            "【本轮标准化证据；只能通过 source_id 引用】",
        )
        self.assertEqual([], topics)
        self.assertIn(
            "证据不足或不相关跳过 1 项",
            path.read_text(encoding="utf-8"),
        )

    def test_planner_query_is_sanitized_before_search_and_collector(self):
        self._write_record()
        searched = []

        def search(query):
            searched.append(query)
            return self._search_result(query)

        def call(prompt, _model_config, **_kwargs):
            if prompt.startswith("[程序每日信息选题任务]"):
                payload = self._planner_payload(
                    prompt,
                    "个人知识管理 user@example.com /mnt/private/a.md",
                )
                return self._response(json.dumps(payload, ensure_ascii=False))
            self.assertNotIn("user@example.com", prompt)
            self.assertNotIn("/mnt/private", prompt)
            return self._response(
                json.dumps(self._collector_payload(prompt), ensure_ascii=False)
            )

        with patch.object(
            information,
            "search_web_once",
            side_effect=search,
        ), patch.object(information, "call_ai", side_effect=call):
            _, success, _ = information.generate_information_briefing(
                datetime.date(2026, 7, 15),
                self.model,
            )

        self.assertTrue(success)
        self.assertIn("[email]", searched[-1])
        self.assertIn("[local-path]", searched[-1])

    def test_briefing_fails_when_web_search_is_not_configured(self):
        settings.CONFIG["third_search"] = {"enabled": False, "api_key": ""}

        message, success, path = information.generate_information_briefing(
            datetime.date(2026, 7, 15),
            self.model,
        )

        self.assertFalse(success)
        self.assertIsNone(path)
        self.assertIn("需要启用第三方搜索", message)

    def test_query_dedup_allows_a_concrete_new_event_in_the_same_field(self):
        result = information._deduplicate_queries(
            [
                {
                    "title": "重复",
                    "query": "个人知识管理最新进展",
                    "reason": "泛化重复",
                    "record_refs": ["R-20260715-001-aaaaaaaaaaaa"],
                },
                {
                    "title": "安全事件",
                    "query": "个人知识管理 Obsidian 插件安全事件影响",
                    "reason": "出现了具体的新事件",
                    "record_refs": ["R-20260715-001-aaaaaaaaaaaa"],
                },
            ],
            [{"query": "个人知识管理 方法研究", "reason": "已有主题"}],
        )

        self.assertEqual(
            ["个人知识管理 Obsidian 插件安全事件影响"],
            [item["query"] for item in result],
        )

    def test_privacy_sanitizer_removes_paths_without_damaging_urls(self):
        value = information._sanitize_text(
            "参考 https://example.com/article，私有文件 /private/note.md",
            300,
        )

        self.assertIn("https://example.com/article", value)
        self.assertNotIn("/private/note.md", value)

    def test_week_context_uses_raw_records_not_summary_and_resets_on_monday(self):
        sunday = settings.DIARY_DIR / "2026-07-19.md"
        monday = settings.DIARY_DIR / "2026-07-20.md"
        sunday.write_text(
            "# 2026-07-19\n\n<summary>周日总结</summary>\n\n"
            "**09:00:** 上周记录\n",
            encoding="utf-8",
        )
        monday.write_text(
            "# 2026-07-20\n\n<summary>暂无今日总结。</summary>\n\n"
            "**09:00:** 周一原始记录\n",
            encoding="utf-8",
        )

        value = information._week_record_context(datetime.date(2026, 7, 20))

        self.assertIn("周一原始记录", value)
        self.assertNotIn("暂无今日总结", value)
        self.assertNotIn("上周记录", value)

    def test_week_context_keeps_newest_records_when_budget_is_small(self):
        monday = settings.DIARY_DIR / "2026-07-13.md"
        tuesday = settings.DIARY_DIR / "2026-07-14.md"
        monday.write_text("**09:00:** 较旧记录-" + "甲" * 80, encoding="utf-8")
        tuesday.write_text("**09:00:** 最新记录-" + "乙" * 80, encoding="utf-8")

        value = information._week_record_context(
            datetime.date(2026, 7, 14),
            limit=120,
        )

        self.assertIn("最新记录", value)
        self.assertNotIn("较旧记录", value)

    def test_period_information_context_reads_only_matching_dates(self):
        directory = settings.ANALYSIS_DIR / "Information"
        directory.mkdir(parents=True)
        (directory / "2026-07-14.md").write_text("本周简报", encoding="utf-8")
        (directory / "2026-07-01.md").write_text("过期简报", encoding="utf-8")

        result = context._information_briefings(
            datetime.date(2026, 7, 13),
            datetime.date(2026, 7, 19),
        )

        self.assertIn("本周简报", result)
        self.assertNotIn("过期简报", result)


if __name__ == "__main__":
    unittest.main()
