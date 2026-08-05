"""分析使用的 OpenAI 兼容请求与工具循环测试。"""

import unittest
from unittest.mock import patch

from AgentRecord import ai_client, settings


class FakeResponse:
    def __init__(self, data):
        self.data = data
        self.status_code = 200
        self.text = ""
        self.response = None

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ai_client.requests.HTTPError(
                str(self.status_code), response=self
            )

    def json(self):
        return self.data


class JournalAITests(unittest.TestCase):
    def setUp(self):
        self.original_third_search = settings.CONFIG.get("third_search")
        settings.CONFIG["third_search"] = {"enabled": False}
        self.model = {
            "name": "test-model",
            "model_id": "test-model-id",
            "api_url": "https://example.test/chat/completions",
            "api_key": "secret",
            "search": False,
        }

    def tearDown(self):
        if self.original_third_search is None:
            settings.CONFIG.pop("third_search", None)
        else:
            settings.CONFIG["third_search"] = self.original_third_search

    @patch("AgentRecord.ai_client.requests.post")
    def test_returns_complete_openai_compatible_response(self, post):
        post.return_value = FakeResponse(
            {
                "choices": [{"message": {"role": "assistant", "content": "最终回答"}}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "prompt_tokens_details": {"cached_tokens": 80},
                },
            }
        )

        response = ai_client.call_ai(
            "问题", self.model
        )
        answer, success, web_count, tool_counts, result_count = response

        self.assertTrue(success)
        self.assertEqual("最终回答", answer)
        self.assertEqual((0, {}, 0), (web_count, tool_counts, result_count))
        payload = post.call_args.kwargs["json"]
        self.assertEqual("test-model-id", payload["model"])
        self.assertNotIn("web_search", payload)
        self.assertEqual(120, response.telemetry["usage"]["total_tokens"])
        self.assertEqual(80, response.telemetry["usage"]["cached_tokens"])
        self.assertEqual(1, response.telemetry["http_attempts"])

    @patch("AgentRecord.ai_client.requests.post")
    def test_reads_deepseek_cache_hit_and_miss_usage(self, post):
        post.return_value = FakeResponse(
            {
                "choices": [
                    {"message": {"role": "assistant", "content": "最终回答"}}
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "prompt_cache_hit_tokens": 70,
                    "prompt_cache_miss_tokens": 30,
                },
            }
        )

        response = ai_client.call_ai("问题", self.model)

        self.assertEqual(70, response.telemetry["usage"]["cached_tokens"])
        self.assertEqual(30, response.telemetry["usage"]["cache_miss_tokens"])

    @patch("AgentRecord.ai_client.execute_tool")
    @patch("AgentRecord.ai_client.requests.post")
    def test_strict_search_preexecutes_allowlist_and_rejects_model_added_query(
        self, post, execute_tool
    ):
        settings.CONFIG["third_search"] = {
            "enabled": True,
            "api_key": "search-key",
            "api_url": "https://search.example.test",
            "max_rounds": 3,
        }
        post.side_effect = [
            FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "search-1",
                                        "type": "function",
                                        "function": {
                                            "name": "web_search",
                                            "arguments": '{"query":"私自改写的查询"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            ),
            FakeResponse(
                {"choices": [{"message": {"role": "assistant", "content": "{}"}}]}
            ),
        ]
        execute_tool.return_value = ai_client.ToolResult(
            "授权查询结果",
            1,
            [
                {
                    "query": "中控给定的查询",
                    "title": "来源",
                    "url": "https://example.com/source",
                }
            ],
        )

        response = ai_client.call_ai(
            "研究",
            self.model,
            allowed_tools={"web_search"},
            allowed_search_queries=["中控给定的查询"],
        )

        self.assertTrue(response.success)
        execute_tool.assert_called_once_with(
            "web_search", {"query": "中控给定的查询"}
        )
        self.assertEqual(["web_search"], response.telemetry["rejected_tool_calls"])
        self.assertEqual(1, response.search_results)

    @patch("AgentRecord.ai_client.execute_tool")
    @patch("AgentRecord.ai_client.requests.post")
    def test_strict_search_executes_each_authorized_query_before_model_call(
        self, post, execute_tool
    ):
        settings.CONFIG["third_search"] = {
            "enabled": True,
            "api_key": "search-key",
            "api_url": "https://search.example.test",
            "max_rounds": 3,
        }
        post.return_value = FakeResponse(
            {"choices": [{"message": {"role": "assistant", "content": "{}"}}]}
        )
        execute_tool.return_value = ai_client.ToolResult("首次结果", 10)

        response = ai_client.call_ai(
            "研究",
            self.model,
            allowed_tools={"web_search"},
            allowed_search_queries=["查询一", "查询二"],
        )

        self.assertTrue(response.success)
        self.assertEqual(2, execute_tool.call_count)
        self.assertEqual(20, response.search_results)
        self.assertEqual(
            ["查询一", "查询二"], response.telemetry["completed_search_queries"]
        )
        request_messages = post.call_args.kwargs["json"]["messages"]
        self.assertIn("查询一", request_messages[-1]["content"])
        self.assertIn("查询二", request_messages[-1]["content"])
        self.assertNotIn("tools", post.call_args.kwargs["json"])

    @patch("AgentRecord.ai_client.execute_tool")
    @patch("AgentRecord.ai_client.requests.post")
    def test_strict_search_prefers_auditable_third_party_for_native_search_model(
        self, post, execute_tool
    ):
        settings.CONFIG["third_search"] = {
            "enabled": True,
            "api_key": "search-key",
            "api_url": "https://search.example.test",
        }
        post.return_value = FakeResponse(
            {"choices": [{"message": {"role": "assistant", "content": "简报"}}]}
        )
        execute_tool.return_value = ai_client.ToolResult("搜索结果", 1)

        response = ai_client.call_ai(
            "严格收集",
            {**self.model, "search": True},
            allowed_tools={"web_search"},
            allowed_search_queries=["固定查询"],
        )

        self.assertTrue(response.success)
        execute_tool.assert_called_once_with(
            "web_search", {"query": "固定查询"}
        )
        payload = post.call_args.kwargs["json"]
        self.assertNotIn("web_search", payload)
        self.assertNotIn("tools", payload)

    @patch("AgentRecord.ai_client._post_with_transient_retry")
    def test_third_party_search_caps_noisy_result_count(self, request):
        settings.CONFIG["third_search"] = {
            "enabled": True,
            "api_key": "search-key",
            "api_url": "https://search.example.test",
            "count": 25,
        }
        request.return_value = FakeResponse(
            {
                "code": 200,
                "data": {
                    "webPages": {
                        "value": [
                            {
                                "name": f"结果 {index}",
                                "url": f"https://example.com/{index}",
                                "snippet": "摘要",
                            }
                            for index in range(15)
                        ]
                    }
                },
            }
        )

        result = ai_client.bocha_search("公开查询")

        self.assertEqual(10, result.result_count)
        self.assertEqual(10, len(result.evidence))
        self.assertEqual(10, request.call_args.kwargs["json"]["count"])

    @patch("AgentRecord.ai_client.requests.post")
    def test_central_permission_can_remove_all_tools(self, post):
        post.return_value = FakeResponse(
            {"choices": [{"message": {"role": "assistant", "content": "JSON"}}]}
        )

        answer, success, _, _, _ = ai_client.call_ai(
            "结构化任务", self.model, allowed_tools=frozenset()
        )

        self.assertTrue(success)
        self.assertEqual("JSON", answer)
        payload = post.call_args.kwargs["json"]
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)

    @patch("AgentRecord.ai_client.execute_tool")
    @patch("AgentRecord.ai_client.requests.post")
    def test_model_cannot_execute_a_tool_absent_from_runtime_allowlist(
        self, post, execute_tool
    ):
        post.side_effect = [
            FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "unauthorized-1",
                                        "type": "function",
                                        "function": {
                                            "name": "read_daily_log",
                                            "arguments": '{"date":"2026-07-14"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            ),
            FakeResponse(
                {"choices": [{"message": {"role": "assistant", "content": "已放弃"}}]}
            ),
        ]

        response = ai_client.call_ai(
            "不允许读日记", self.model, allowed_tools=frozenset()
        )

        self.assertTrue(response.success)
        execute_tool.assert_not_called()
        self.assertEqual(
            ["read_daily_log"], response.telemetry["rejected_tool_calls"]
        )
        refusal = post.call_args_list[1].kwargs["json"]["messages"][-1]
        self.assertIn("未获中控授权", refusal["content"])

    @patch("AgentRecord.ai_client.requests.post")
    def test_structured_output_uses_configured_json_mode(self, post):
        post.return_value = FakeResponse(
            {"choices": [{"message": {"role": "assistant", "content": "{}"}}]}
        )
        model = {**self.model, "json_mode": True, "max_tokens": 32768}

        response = ai_client.call_ai("JSON 任务", model, structured_output=True)

        self.assertTrue(response.success)
        payload = post.call_args.kwargs["json"]
        self.assertEqual({"type": "json_object"}, payload["response_format"])
        self.assertEqual(32768, payload["max_tokens"])

    @patch("AgentRecord.ai_client.execute_tool")
    @patch("AgentRecord.ai_client.requests.post")
    def test_executes_tool_call_then_requests_final_answer(self, post, execute_tool):
        post.side_effect = [
            FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "read_daily_log",
                                            "arguments": '{"date":"2026-07-14"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            ),
            FakeResponse(
                {"choices": [{"message": {"role": "assistant", "content": "工具后回答"}}]}
            ),
        ]
        execute_tool.return_value = ("日记内容", 0)

        answer, success, _, tool_counts, _ = ai_client.call_ai("分析任务", self.model)

        self.assertTrue(success)
        self.assertEqual("工具后回答", answer)
        self.assertEqual({"read_daily_log": 1}, tool_counts)
        self.assertEqual(2, post.call_count)
        second_payload = post.call_args_list[1].kwargs["json"]
        self.assertEqual("tool", second_payload["messages"][-1]["role"])
        self.assertEqual("日记内容", second_payload["messages"][-1]["content"])

    @patch("AgentRecord.ai_client.execute_tool")
    @patch("AgentRecord.ai_client.requests.post")
    def test_malformed_tool_arguments_are_returned_for_model_correction(
        self, post, execute_tool
    ):
        post.side_effect = [
            FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "bad-call",
                                        "type": "function",
                                        "function": {
                                            "name": "read_daily_log",
                                            "arguments": "{bad json",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            ),
            FakeResponse(
                {"choices": [{"message": {"role": "assistant", "content": "已修正"}}]}
            ),
        ]

        response = ai_client.call_ai("分析任务", self.model)

        self.assertTrue(response.success)
        self.assertEqual("已修正", response.text)
        execute_tool.assert_not_called()
        correction = post.call_args_list[1].kwargs["json"]["messages"][-1]
        self.assertEqual("tool", correction["role"])
        self.assertIn("工具参数格式错误", correction["content"])

    @patch("AgentRecord.ai_client.execute_tool")
    @patch("AgentRecord.ai_client.requests.post")
    def test_thinking_tool_round_preserves_reasoning_content(
        self, post, execute_tool
    ):
        post.side_effect = [
            FakeResponse(
                {
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "reasoning_content": "先读取记录再回答",
                                "tool_calls": [
                                    {
                                        "id": "read-1",
                                        "type": "function",
                                        "function": {
                                            "name": "read_daily_log",
                                            "arguments": '{"date":"2026-07-14"}',
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                }
            ),
            FakeResponse(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "完成"},
                        }
                    ]
                }
            ),
        ]
        execute_tool.return_value = ai_client.ToolResult("记录", 0)

        response = ai_client.call_ai("分析任务", self.model)

        self.assertTrue(response.success)
        messages = post.call_args_list[1].kwargs["json"]["messages"]
        assistant = next(message for message in messages if message["role"] == "assistant")
        self.assertEqual("先读取记录再回答", assistant["reasoning_content"])
        self.assertNotIn("tool_choice", post.call_args_list[0].kwargs["json"])

    @patch("AgentRecord.ai_client.requests.post")
    def test_output_length_stop_is_classified_as_truncation(self, post):
        post.return_value = FakeResponse(
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": '{"partial":'},
                    }
                ]
            }
        )

        response = ai_client.call_ai("结构化任务", self.model)

        self.assertFalse(response.success)
        self.assertIn(ai_client.OUTPUT_TRUNCATED_MARKER, response.text)
        self.assertEqual(["length"], response.telemetry["finish_reasons"])

    @patch("AgentRecord.ai_client.requests.post")
    def test_empty_stop_response_gets_one_final_answer_retry(self, post):
        post.side_effect = [
            FakeResponse(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "reasoning_content": "内部思考",
                            },
                        }
                    ]
                }
            ),
            FakeResponse(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "最终回答"},
                        }
                    ]
                }
            ),
        ]

        response = ai_client.call_ai("结构化任务", self.model)

        self.assertTrue(response.success)
        self.assertEqual("最终回答", response.text)
        self.assertEqual(2, post.call_count)
        self.assertEqual(1, response.telemetry["empty_content_retries"])
        retry_message = post.call_args_list[1].kwargs["json"]["messages"][-1]
        self.assertEqual("user", retry_message["role"])
        self.assertIn("没有返回最终正文", retry_message["content"])

    @patch("AgentRecord.ai_client.requests.post")
    def test_repeated_empty_stop_response_still_fails_boundedly(self, post):
        post.return_value = FakeResponse(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": ""},
                    }
                ]
            }
        )

        response = ai_client.call_ai("结构化任务", self.model)

        self.assertFalse(response.success)
        self.assertEqual("(AI 未给出最终回答)", response.text)
        self.assertEqual(2, post.call_count)
        self.assertEqual(1, response.telemetry["empty_content_retries"])

    @patch("AgentRecord.ai_client.requests.post")
    def test_filtered_and_resource_exhausted_finishes_are_failures(self, post):
        post.side_effect = [
            FakeResponse(
                {
                    "choices": [
                        {
                            "finish_reason": "content_filter",
                            "message": {"role": "assistant", "content": "部分内容"},
                        }
                    ]
                }
            ),
            FakeResponse(
                {
                    "choices": [
                        {
                            "finish_reason": "insufficient_system_resource",
                            "message": {"role": "assistant", "content": ""},
                        }
                    ]
                }
            ),
        ]

        filtered = ai_client.call_ai("结构化任务", self.model)
        exhausted = ai_client.call_ai("结构化任务", self.model)

        self.assertFalse(filtered.success)
        self.assertIn(ai_client.OUTPUT_FILTERED_MARKER, filtered.text)
        self.assertFalse(exhausted.success)
        self.assertTrue(ai_client.is_network_failure(exhausted.text))

    def test_system_prompt_is_for_analysis_not_realtime_chat(self):
        prompt = ai_client._build_system_prompt()

        self.assertIn("分析引擎", prompt)
        self.assertIn("不承担日常聊天", prompt)
        self.assertNotIn("@AI 提问", prompt)

    def test_incomplete_third_party_search_config_is_not_available(self):
        settings.CONFIG["third_search"] = {
            "enabled": True,
            "api_key": "search-key",
        }

        self.assertFalse(ai_client.third_party_search_available())

    @patch("AgentRecord.ai_client.time.sleep")
    @patch("AgentRecord.ai_client.requests.post")
    def test_transient_connection_errors_use_bounded_retry(self, post, sleep):
        expected = FakeResponse({"ok": True})
        post.side_effect = [
            ai_client.requests.ConnectionError("dns"),
            ai_client.requests.Timeout("timeout"),
            expected,
        ]

        response = ai_client._post_with_transient_retry("https://example.test")

        self.assertIs(expected, response)
        self.assertEqual(3, post.call_count)
        self.assertEqual([1, 2], [call.args[0] for call in sleep.call_args_list])

    @patch("AgentRecord.ai_client.time.sleep")
    @patch("AgentRecord.ai_client.requests.post")
    def test_transient_server_errors_use_bounded_retry(self, post, sleep):
        unavailable = FakeResponse({})
        unavailable.status_code = 503
        expected = FakeResponse({"ok": True})
        post.side_effect = [unavailable, unavailable, expected]

        response = ai_client._post_with_transient_retry("https://example.test")

        self.assertIs(expected, response)
        self.assertEqual(3, post.call_count)
        self.assertEqual([1, 2], [call.args[0] for call in sleep.call_args_list])

    @patch("AgentRecord.ai_client.time.sleep")
    @patch("AgentRecord.ai_client.requests.post")
    def test_exhausted_connection_errors_are_marked_as_network_failure(
        self, post, sleep
    ):
        post.side_effect = ai_client.requests.ConnectionError("dns")

        message, success, _, _, _ = ai_client.call_ai("自动任务", self.model)

        self.assertFalse(success)
        self.assertTrue(ai_client.is_network_failure(message))
        self.assertEqual(3, post.call_count)
        self.assertEqual([1, 2], [call.args[0] for call in sleep.call_args_list])

    @patch("AgentRecord.ai_client.time.sleep")
    @patch("AgentRecord.ai_client.requests.post")
    def test_rate_limit_and_auth_errors_are_classified_separately(self, post, sleep):
        rate_limited = FakeResponse({})
        rate_limited.status_code = 429
        post.return_value = rate_limited

        message, success, _, _, _ = ai_client.call_ai("自动任务", self.model)
        self.assertFalse(success)
        self.assertTrue(ai_client.is_rate_limit_failure(message))
        self.assertEqual(1, post.call_count)

        post.reset_mock()
        unauthorized = FakeResponse({})
        unauthorized.status_code = 401
        post.return_value = unauthorized
        message, success, _, _, _ = ai_client.call_ai("自动任务", self.model)
        self.assertFalse(success)
        self.assertTrue(ai_client.is_config_failure(message))
        self.assertEqual(1, post.call_count)


if __name__ == "__main__":
    unittest.main()
