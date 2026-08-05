import unittest

from AgentRecord.agents import (
    AGENTS,
    researcher,
    research_planner,
    retrospective,
    reviewer,
)
from AgentRecord.agents.base import AgentPipelineError, _prompt, invoke_agent
from AgentRecord.ai_client import AIResponse


class AgentModuleTests(unittest.TestCase):
    def test_four_agents_have_separate_responsibilities(self):
        self.assertEqual(
            {"retrospective", "research_planner", "researcher", "reviewer"},
            set(AGENTS),
        )
        self.assertTrue(AGENTS["reviewer"].can_read_raw)
        self.assertFalse(AGENTS["researcher"].can_read_raw)

    def test_agent_prompt_requires_one_minimal_json_task(self):
        prompt = _prompt(
            retrospective.SPEC,
            "生成正文",
            {"records": ["内容"]},
        )
        self.assertIn("只负责当前这一项语义任务", prompt)
        self.assertIn("完整覆盖本任务所需信息的前提下保持简洁", prompt)
        self.assertIn("一个最小 JSON 对象", prompt)
        self.assertIn("不得自行增加数组", prompt)

    def test_agent_invocation_uses_structured_output_for_minimal_object(self):
        calls = []

        def fake_call(prompt, model, **kwargs):
            calls.append(kwargs)
            return AIResponse(
                '{"text":"纯文本正文"}',
                True,
                {"usage": {"total_tokens": 3}},
            )

        payload, telemetry = invoke_agent(
            retrospective.SPEC,
            "生成正文",
            {"records": []},
            {"name": "mock"},
            fake_call,
        )

        self.assertEqual({"text": "纯文本正文"}, payload)
        self.assertEqual(3, telemetry["usage"]["total_tokens"])
        self.assertEqual([{"structured_output": True}], calls)

    def test_agent_invocation_unwraps_one_outer_json_fence(self):
        payload, _ = invoke_agent(
            retrospective.SPEC,
            "生成正文",
            {},
            {"name": "mock"},
            lambda *args, **kwargs: AIResponse(
                '```json\n{"text":"正文"}\n```', True
            ),
        )
        self.assertEqual({"text": "正文"}, payload)

    def test_retrospective_accepts_only_unstructured_text(self):
        self.assertEqual(
            "正文第一段\n\n正文第二段",
            retrospective.validate({"text": "正文第一段\n\n正文第二段"}),
        )
        with self.assertRaisesRegex(AgentPipelineError, "不得自行输出标题"):
            retrospective.validate({"text": "### 模型自拟标题\n正文"})
        with self.assertRaisesRegex(AgentPipelineError, "字符串 text"):
            retrospective.validate({"text": ["第一段", "第二段"]})
        with self.assertRaisesRegex(AgentPipelineError, "不得自行输出 URL"):
            retrospective.validate({"text": "正文 https://example.com"})

    def test_planner_returns_one_query_or_skip_and_sanitizes_private_data(self):
        self.assertIsNone(
            research_planner.normalize_query({"action": "skip", "query": ""})
        )
        query = research_planner.normalize_query(
            {
                "action": "search",
                "query": "如何理解 test@example.com 与 /home/user/private 中的长期记录方法？",
            }
        )
        self.assertIn("[email]", query)
        self.assertIn("[local-path]", query)
        self.assertNotIn("test@example.com", query)

    def test_semantic_outputs_are_not_rejected_or_truncated_by_length(self):
        retrospective_text = "回顾正文" * 10000
        research_text = "研究正文" * 10000
        feedback = "具体修改意见" * 2000
        query = "公开研究问题" * 100

        self.assertEqual(
            retrospective_text,
            retrospective.validate({"text": retrospective_text}),
        )
        self.assertEqual(
            ("supported", research_text),
            researcher.validate({"status": "supported", "text": research_text}),
        )
        self.assertEqual(
            (False, feedback),
            reviewer.validate({"approved": False, "feedback": feedback}),
        )
        self.assertEqual(
            query,
            research_planner.normalize_query(
                {"action": "search", "query": query}
            ),
        )

    def test_researcher_renders_controller_owned_heading_and_sources(self):
        topic = {
            "topic_id": "Q001",
            "title": "记录方法的研究边界",
            "source_refs": ["R-20260714-001-aaaaaaaaaaaa"],
        }
        evidence = [
            {
                "source_id": "W-Q001-001",
                "topic_id": "Q001",
                "title": "研究来源",
                "url": "https://example.com/source",
                "published": "2026-07-14",
            }
        ]
        markdown, sources = researcher.render_topic("分析正文。", topic, evidence)
        self.assertIn("### 记录方法的研究边界", markdown)
        self.assertIn("分析正文。", markdown)
        self.assertIn("R-20260714-001-aaaaaaaaaaaa", markdown)
        self.assertIn("https://example.com/source", markdown)
        self.assertEqual("W-Q001-001", sources[0]["source_id"])

    def test_researcher_rejects_model_written_url(self):
        with self.assertRaisesRegex(AgentPipelineError, "不得自行输出 URL"):
            researcher.validate(
                {"status": "supported", "text": "请看 https://model.example"}
            )

    def test_all_agent_contracts_reject_model_owned_arrays(self):
        with self.assertRaises(AgentPipelineError):
            research_planner.normalize_query(
                {"action": "search", "query": ["问题一", "问题二"]}
            )
        with self.assertRaises(AgentPipelineError):
            researcher.validate(
                {"status": "supported", "text": ["分析一", "分析二"]}
            )
        with self.assertRaises(AgentPipelineError):
            reviewer.validate({"approved": True, "feedback": []})

    def test_agent_contracts_reject_model_owned_structure_fields(self):
        with self.assertRaises(AgentPipelineError):
            retrospective.validate({"text": "正文", "sections": []})
        with self.assertRaisesRegex(AgentPipelineError, "单个问题"):
            research_planner.normalize_query(
                {"action": "search", "query": "问题一？\n问题二？"}
            )
        with self.assertRaises(AgentPipelineError):
            researcher.validate(
                {
                    "status": "supported",
                    "text": "分析",
                    "sources": ["W-Q001-001"],
                }
            )

    def test_reviewer_uses_minimal_json_decision(self):
        self.assertEqual(
            (True, ""), reviewer.validate({"approved": True, "feedback": ""})
        )
        self.assertEqual(
            (False, "这一判断没有记录支持"),
            reviewer.validate(
                {"approved": False, "feedback": "这一判断没有记录支持"}
            ),
        )

    def test_revision_prompt_preserves_original_request_as_prefix(self):
        original = _prompt(retrospective.SPEC, "生成", {"records": ["内容"]})
        revised = _prompt(
            retrospective.SPEC,
            "生成",
            {"records": ["内容"]},
            {"feedback": "删去无依据判断"},
        )
        self.assertTrue(revised.startswith(original.rsplit("\n\n只输出职责", 1)[0]))
        self.assertIn("删去无依据判断", revised)


if __name__ == "__main__":
    unittest.main()
