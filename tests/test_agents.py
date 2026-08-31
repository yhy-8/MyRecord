import unittest

from server.ai.agents import (
    AGENTS,
    REPORT_SPEC,
    AgentPipelineError,
    _prompt,
    invoke_agent,
    is_json_container,
)
from server.ai.ai_client import AIResponse


class AgentModuleTests(unittest.TestCase):
    def test_agents_have_only_the_single_report_agent(self):
        # 已取消 retrospective / reviewer：现在只有单个 Report Agent。
        self.assertEqual({"report"}, set(AGENTS))
        self.assertTrue(AGENTS["report"].can_read_raw)

    def test_report_agent_prompt_mentions_citation_and_no_source_table(self):
        prompt = _prompt(REPORT_SPEC, "生成周报正文", {"records": [{"n": 1, "date": "2026-07-14", "line": 23, "text": "内容"}]})
        self.assertIn("只负责当前这一项语义任务", prompt)
        self.assertIn("完整覆盖本任务所需信息的前提下保持简洁", prompt)
        self.assertIn("数字引用", prompt)
        self.assertIn("不要生成文末来源表", prompt)
        self.assertIn("R-", prompt)  # 明确禁止输出 R- 来源标识

    def test_report_invocation_is_plain_text_with_thinking_budget(self):
        calls = []

        def fake_call(prompt, model, **kwargs):
            calls.append(kwargs)
            return AIResponse(
                "## 本周回顾\n- 完成记录模块重构。[1]", True
            )

        body, _ = invoke_agent(
            REPORT_SPEC,
            "生成周报正文",
            {"records": [{"n": 1, "date": "2026-07-14", "line": 23, "text": "内容"}]},
            {"name": "mock"},
            fake_call,
        )

        self.assertEqual("## 本周回顾\n- 完成记录模块重构。[1]", body)
        self.assertEqual(
            [{"structured_output": False, "thinking": True, "max_tokens": 65536}],
            calls,
        )

    def test_report_invocation_reports_token_telemetry(self):
        def fake_call(prompt, model, **kwargs):
            return AIResponse(
                "正文",
                True,
                {"usage": {"total_tokens": 12, "prompt_tokens": 5}},
            )

        _, telemetry = invoke_agent(
            REPORT_SPEC, "生成", {"records": []}, {"name": "mock"}, fake_call
        )
        self.assertEqual(12, telemetry["usage"]["total_tokens"])

    def test_report_invocation_raises_on_unfinished_output(self):
        def fake_call(prompt, model, **kwargs):
            return AIResponse("正文\n输出截断: 达到长度上限", False)

        with self.assertRaises(AgentPipelineError):
            invoke_agent(
                REPORT_SPEC, "生成", {"records": []}, {"name": "mock"}, fake_call
            )

    def test_is_json_container(self):
        self.assertTrue(is_json_container('{"a": 1}'))
        self.assertFalse(is_json_container("正文"))


if __name__ == "__main__":
    unittest.main()