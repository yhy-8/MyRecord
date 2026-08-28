import unittest

from server.ai.agents import AGENTS, retrospective, reviewer
from server.ai.agents.base import AgentPipelineError, _prompt, invoke_agent
from server.ai.ai_client import AIResponse


class AgentModuleTests(unittest.TestCase):
    def test_agents_have_separate_responsibilities(self):
        self.assertEqual({"retrospective", "reviewer"}, set(AGENTS))
        self.assertTrue(AGENTS["reviewer"].can_read_raw)
        self.assertTrue(AGENTS["retrospective"].can_read_raw)

    def test_agent_prompt_requires_one_minimal_json_task(self):
        prompt = _prompt(reviewer.SPEC, "审查正文", {"text": "正文"})
        self.assertIn("只负责当前这一项语义任务", prompt)
        self.assertIn("完整覆盖本任务所需信息的前提下保持简洁", prompt)
        self.assertIn("一个最小 JSON 对象", prompt)
        self.assertIn("不得自行增加数组", prompt)

    def test_agent_invocation_uses_structured_output_for_minimal_object(self):
        calls = []

        def fake_call(prompt, model, **kwargs):
            calls.append(kwargs)
            return AIResponse(
                '{"approved":true,"feedback":""}',
                True,
                {"usage": {"total_tokens": 3}},
            )

        payload, telemetry = invoke_agent(
            reviewer.SPEC,
            "审查这一份正文，并按最小对象返回结论和一段修改意见。",
            {"text": "正文"},
            {"name": "mock"},
            fake_call,
        )

        self.assertEqual({"approved": True, "feedback": ""}, payload)
        self.assertEqual(3, telemetry["usage"]["total_tokens"])
        self.assertEqual(
            [
                {
                    "structured_output": True,
                    "thinking": True,
                    "max_tokens": 16384,
                }
            ],
            calls,
        )

    def test_agent_invocation_unwraps_one_outer_json_fence(self):
        payload, _ = invoke_agent(
            reviewer.SPEC,
            "审查这一份正文，并按最小对象返回结论和一段修改意见。",
            {"text": "正文"},
            {"name": "mock"},
            lambda *args, **kwargs: AIResponse(
                '```json\n{"approved":true,"feedback":""}\n```', True
            ),
        )
        self.assertEqual({"approved": True, "feedback": ""}, payload)

    def test_json_protocol_error_gets_one_bounded_retry(self):
        responses = iter(
            [
                AIResponse("not json", True, {"usage": {"total_tokens": 2}}),
                AIResponse(
                    '{"approved":true,"feedback":""}',
                    True,
                    {"usage": {"total_tokens": 3}},
                ),
            ]
        )
        prompts = []

        def fake_call(prompt, model, **kwargs):
            prompts.append(prompt)
            return next(responses)

        payload, telemetry = invoke_agent(
            reviewer.SPEC, "审查正文", {}, {"name": "mock"}, fake_call
        )

        self.assertEqual({"approved": True, "feedback": ""}, payload)
        self.assertEqual(2, len(prompts))
        self.assertIn("协议重试", prompts[1])
        self.assertEqual(1, telemetry["protocol_retries"])
        self.assertEqual(5, telemetry["usage"]["total_tokens"])

    def test_retrospective_invocation_is_plain_text_with_thinking_budget(self):
        calls = []

        def fake_call(prompt, model, **kwargs):
            calls.append(kwargs)
            return AIResponse("纯文本正文", True)

        body, _ = invoke_agent(
            retrospective.SPEC, "生成正文", {}, {"name": "mock"}, fake_call
        )

        self.assertEqual("纯文本正文", body)
        self.assertEqual(
            [{"structured_output": False, "thinking": True, "max_tokens": 65536}],
            calls,
        )

    def test_retrospective_accepts_structured_bullet_output(self):
        # 新版整理与回顾要求分点排版，允许 Markdown 无序列表。
        bullets = (
            "**工作进展**\n"
            "- 完成记录模块重构。\n"
            "- 修复同步丢帧问题。\n"
            "\n"
            "**遇到的问题**\n"
            "- 周报探索耗时过长，已移除。"
        )
        self.assertEqual(bullets, retrospective.validate(bullets))
        self.assertEqual(
            "正文第一段\n\n正文第二段",
            retrospective.validate("正文第一段\n\n正文第二段"),
        )

    def test_retrospective_rejects_only_structural_violations(self):
        with self.assertRaisesRegex(AgentPipelineError, "不得自行输出标题"):
            retrospective.validate("### 模型自拟标题\n正文")
        with self.assertRaisesRegex(AgentPipelineError, "纯文本"):
            retrospective.validate(["第一段", "第二段"])
        with self.assertRaisesRegex(AgentPipelineError, "不得自行输出 URL"):
            retrospective.validate("正文 https://example.com")
        with self.assertRaisesRegex(AgentPipelineError, "不得输出 JSON"):
            retrospective.validate('{"text":"正文"}')

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

    def test_all_agent_contracts_reject_model_owned_arrays(self):
        with self.assertRaises(AgentPipelineError):
            reviewer.validate({"approved": True, "feedback": []})

    def test_revision_prompt_preserves_original_request_as_prefix(self):
        revised = _prompt(
            retrospective.SPEC,
            "生成",
            {"records": ["内容"]},
            {"feedback": "删去无依据判断"},
        )
        self.assertIn("【本次任务】", revised)
        self.assertIn("删去无依据判断", revised)
        self.assertIn("【中控修订请求】", revised)


if __name__ == "__main__":
    unittest.main()