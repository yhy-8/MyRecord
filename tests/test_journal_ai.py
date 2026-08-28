"""OpenAI-compatible model request and audit telemetry tests."""

import unittest
from unittest.mock import patch

from server.ai import ai_client, settings


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
        self.model = {
            "name": "test-model",
            "model_id": "test-model-id",
            "api_url": "https://example.test/chat/completions",
            "api_key": "secret",
        }

    @patch("server.ai.ai_client.requests.post")
    def test_returns_complete_openai_compatible_response(self, post):
        post.return_value = FakeResponse(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "最终回答"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "prompt_tokens_details": {"cached_tokens": 80},
                },
            }
        )

        response = ai_client.call_ai("问题", self.model)
        answer, success = response

        self.assertTrue(success)
        self.assertEqual("最终回答", answer)
        payload = post.call_args.kwargs["json"]
        self.assertEqual("test-model-id", payload["model"])
        self.assertNotIn("tools", payload)
        self.assertEqual(120, response.telemetry["usage"]["total_tokens"])
        self.assertEqual(80, response.telemetry["usage"]["cached_tokens"])
        self.assertEqual(1, response.telemetry["http_attempts"])

    @patch("server.ai.ai_client.requests.post")
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

    @patch("server.ai.ai_client.requests.post")
    def test_malformed_usage_does_not_discard_valid_content(self, post):
        post.return_value = FakeResponse(
            {
                "choices": [
                    {"message": {"role": "assistant", "content": "最终回答"}}
                ],
                "usage": {
                    "prompt_tokens": "invalid",
                    "total_tokens": None,
                    "prompt_tokens_details": "invalid",
                },
            }
        )

        response = ai_client.call_ai("问题", self.model)

        self.assertTrue(response.success)
        self.assertEqual(0, response.telemetry["usage"]["prompt_tokens"])

    @patch("server.ai.ai_client.requests.post")
    def test_structured_output_uses_configured_json_mode(self, post):
        post.return_value = FakeResponse(
            {"choices": [{"message": {"role": "assistant", "content": "{}"}}]}
        )
        model = {**self.model, "json_mode": True, "max_tokens": 100000}

        response = ai_client.call_ai("JSON 任务", model, structured_output=True)

        self.assertTrue(response.success)
        payload = post.call_args.kwargs["json"]
        self.assertEqual({"type": "json_object"}, payload["response_format"])
        self.assertEqual(100000, payload["max_tokens"])

    @patch("server.ai.ai_client.requests.post")
    def test_deepseek_task_controls_thinking_effort_and_budget(self, post):
        post.return_value = FakeResponse(
            {"choices": [{"message": {"role": "assistant", "content": "正文"}}]}
        )
        model = {
            **self.model,
            "api_url": "https://api.deepseek.com/chat/completions",
            "temperature": 0.7,
        }

        ai_client.call_ai("回顾", model, thinking=True, max_tokens=65536)

        payload = post.call_args.kwargs["json"]
        self.assertEqual({"type": "enabled"}, payload["thinking"])
        self.assertEqual("high", payload["reasoning_effort"])
        self.assertEqual(65536, payload["max_tokens"])
        self.assertNotIn("temperature", payload)

    @patch("server.ai.ai_client.requests.post")
    def test_deepseek_non_thinking_task_is_explicit(self, post):
        post.return_value = FakeResponse(
            {"choices": [{"message": {"role": "assistant", "content": "正文"}}]}
        )
        model = {
            **self.model,
            "api_url": "https://api.deepseek.com/chat/completions",
            "temperature": 0.2,
        }

        ai_client.call_ai("总结", model, thinking=False, max_tokens=4096)

        payload = post.call_args.kwargs["json"]
        self.assertEqual({"type": "disabled"}, payload["thinking"])
        self.assertNotIn("reasoning_effort", payload)
        self.assertEqual(0.2, payload["temperature"])
        self.assertEqual(4096, payload["max_tokens"])

    @patch("server.ai.ai_client.requests.post")
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

    @patch("server.ai.ai_client.requests.post")
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
                            "message": {
                                "role": "assistant",
                                "content": "最终回答",
                            },
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

    @patch("server.ai.ai_client.requests.post")
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

    @patch("server.ai.ai_client.requests.post")
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

    def test_system_prompt_is_for_controller_supplied_analysis(self):
        prompt = ai_client._build_system_prompt()

        self.assertIn("分析引擎", prompt)
        self.assertIn("不承担日常聊天", prompt)
        self.assertIn("不自行读取文件或调用工具", prompt)

    def test_missing_model_key_is_a_configuration_failure(self):
        model = {
            "name": "test",
            "api_url": "https://example.test/v1",
            "api_key": None,
        }

        response = ai_client.call_ai("问题", model)

        self.assertFalse(response.success)
        self.assertTrue(ai_client.is_config_failure(response.text))

    def test_invalid_retry_policy_is_a_configuration_failure(self):
        with patch.dict(
            settings.CONFIG,
            {"retry": {"empty_response_retry_limit": 2}},
        ):
            response = ai_client.call_ai("问题", self.model)

        self.assertFalse(response.success)
        self.assertTrue(ai_client.is_config_failure(response.text))

    @patch("server.ai.ai_client.time.sleep")
    @patch("server.ai.ai_client.requests.post")
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

    @patch("server.ai.ai_client.time.sleep")
    @patch("server.ai.ai_client.requests.post")
    def test_transient_http_retry_count_and_backoff_are_configurable(
        self, post, sleep
    ):
        expected = FakeResponse({"ok": True})
        post.side_effect = [ai_client.requests.Timeout("timeout"), expected]

        with patch.dict(
            settings.CONFIG,
            {
                "retry": {
                    "transient_http_retry_limit": 1,
                    "transient_http_backoff_seconds": 3,
                }
            },
        ):
            response = ai_client._post_with_transient_retry(
                "https://example.test"
            )

        self.assertIs(expected, response)
        self.assertEqual(2, post.call_count)
        sleep.assert_called_once_with(3)

    @patch("server.ai.ai_client.requests.post")
    def test_empty_response_retry_can_be_disabled(self, post):
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

        with patch.dict(
            settings.CONFIG,
            {"retry": {"empty_response_retry_limit": 0}},
        ):
            response = ai_client.call_ai("问题", self.model)

        self.assertFalse(response.success)
        self.assertEqual(1, post.call_count)

    @patch("server.ai.ai_client.time.sleep")
    @patch("server.ai.ai_client.requests.post")
    def test_transient_server_errors_use_bounded_retry(self, post, sleep):
        unavailable = FakeResponse({})
        unavailable.status_code = 503
        expected = FakeResponse({"ok": True})
        post.side_effect = [unavailable, unavailable, expected]

        response = ai_client._post_with_transient_retry("https://example.test")

        self.assertIs(expected, response)
        self.assertEqual(3, post.call_count)
        self.assertEqual([1, 2], [call.args[0] for call in sleep.call_args_list])

    @patch("server.ai.ai_client.time.sleep")
    @patch("server.ai.ai_client.requests.post")
    def test_exhausted_connection_errors_are_marked_as_network_failure(
        self, post, sleep
    ):
        post.side_effect = ai_client.requests.ConnectionError("dns")

        message, success = ai_client.call_ai("自动任务", self.model)

        self.assertFalse(success)
        self.assertTrue(ai_client.is_network_failure(message))
        self.assertEqual(3, post.call_count)
        self.assertEqual([1, 2], [call.args[0] for call in sleep.call_args_list])


if __name__ == "__main__":
    unittest.main()