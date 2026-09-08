"""AI 客户端错误分类与异常路径单元测试。

补足 ai_client.call_ai 的异常处理分支：
- HTTP 错误分类：429（限流）/ 401、403（配置错）/ 5xx（瞬时网络）/ 其他 4xx（接口异常）
- requests.RequestException / 泛异常兜底
- 活动模型配置缺失（非 dict / 空 model_id / 无效 api_url / 空 api_key）
- 输出截断 / 内容过滤 / 资源不足 / 空正文
"""

import unittest
from unittest.mock import patch

from server.ai import ai_client
from server.ai.ai_client import (
    AIResponse,
    CONFIG_ERROR_MARKER,
    NETWORK_ERROR_MARKER,
    OUTPUT_FILTERED_MARKER,
    OUTPUT_TRUNCATED_MARKER,
    RATE_LIMIT_ERROR_MARKER,
    call_ai,
    is_config_failure,
    is_network_failure,
)


class _FakeHTTPResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class _RaisingResponse:
    """在 raise_for_status 时抛 HTTPError，并带 response 供分类。"""

    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text
        self.response = _FakeHTTPResponse(status_code, text)

    def raise_for_status(self):
        raise ai_client.requests.HTTPError(
            f"{self.status_code} error", response=self
        )


class CallAIConfigFailureTests(unittest.TestCase):
    def setUp(self):
        self.model = {
            "name": "test-model",
            "model_id": "test-model-id",
            "api_url": "https://example.test/v1",
            "api_key": "sk-x",
        }

    def test_model_config_not_a_dict_is_config_failure(self):
        response = call_ai("问题", "not-a-dict")
        self.assertFalse(response.success)
        self.assertEqual(CONFIG_ERROR_MARKER, response.text.split(" ")[0])

    def test_missing_model_id_is_config_failure(self):
        model = {**self.model, "model_id": "", "name": ""}
        response = call_ai("问题", model)
        self.assertFalse(response.success)
        self.assertTrue(is_config_failure(response.text))

    def test_invalid_api_url_is_config_failure(self):
        for bad in ("not-a-url", "ftp://host", "https://", "http://"):
            model = {**self.model, "api_url": bad}
            response = call_ai("问题", model)
            self.assertFalse(response.success, f"应判为配置失败: {bad!r}")
            self.assertTrue(is_config_failure(response.text))

    def test_missing_api_key_is_config_failure(self):
        model = {**self.model, "api_key": ""}
        response = call_ai("问题", model)
        self.assertFalse(response.success)
        self.assertTrue(is_config_failure(response.text))

    def test_valid_config_without_network_reports_network_error(self):
        # api_url 无法连到（用立刻失败的 timeout/拒绝），应归为网络异常
        model = {**self.model, "api_url": "https://127.0.0.1:1/never/there"}
        with patch("server.ai.ai_client.requests.post",
                   side_effect=ai_client.requests.ConnectionError("conn refused")):
            response = call_ai("问题", model)
        self.assertFalse(response.success)
        self.assertTrue(is_network_failure(response.text))


class HTTPErrorClassificationTests(unittest.TestCase):
    def setUp(self):
        self.model = {
            "name": "m", "model_id": "m", "api_url": "https://example.test/v1",
            "api_key": "sk-x",
        }
        self.ok_choices = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {},
        }

    @patch("server.ai.ai_client.requests.post")
    def test_429_is_rate_limit(self, post):
        post.return_value = _RaisingResponse(429, "rate limited")
        response = call_ai("问题", self.model)
        self.assertFalse(response.success)
        self.assertIn(RATE_LIMIT_ERROR_MARKER, response.text)

    @patch("server.ai.ai_client.requests.post")
    def test_401_403_are_config_failure(self, post):
        for code in (401, 403):
            post.return_value = _RaisingResponse(code, "forbidden")
            response = call_ai("问题", self.model)
            self.assertFalse(response.success)
            self.assertTrue(is_config_failure(response.text), f"status {code}")

    @patch("server.ai.ai_client.requests.post")
    def test_5xx_is_network_failure(self, post):
        for code in (500, 503, 502):
            post.return_value = _RaisingResponse(code, "internal")
            response = call_ai("问题", self.model)
            self.assertFalse(response.success)
            self.assertTrue(is_network_failure(response.text), f"status {code}")

    @patch("server.ai.ai_client.requests.post")
    def test_other_4xx_is_interface_error(self, post):
        post.return_value = _RaisingResponse(400, "bad request")
        response = call_ai("问题", self.model)
        self.assertFalse(response.success)
        self.assertNotIn(NETWORK_ERROR_MARKER, response.text)
        self.assertIn("接口异常", response.text)
        # 错误响应体被附加到消息里，便于诊断
        self.assertIn("bad request", response.text)

    @patch("server.ai.ai_client.requests.post")
    def test_http_error_without_response_is_interface_error(self, post):
        def raise_noresp(_url, **_kw):
            raise ai_client.requests.HTTPError("boom")

        # HTTPError 无 response 时的兜底
        post.side_effect = lambda *a, **k: (_ for _ in ()).throw(
            ai_client.requests.HTTPError("boom")
        )
        response = call_ai("问题", self.model)
        self.assertFalse(response.success)

    @patch("server.ai.ai_client.requests.post")
    def test_generic_request_exception_is_interface_error(self, post):
        post.side_effect = ai_client.requests.TooManyRedirects("redirect loop")
        response = call_ai("问题", self.model)
        self.assertFalse(response.success)
        self.assertIn("接口异常", response.text)

    @patch("server.ai.ai_client.requests.post")
    def test_unexpected_exception_is_interface_error(self, post):
        post.side_effect = RuntimeError("weird")
        response = call_ai("问题", self.model)
        self.assertFalse(response.success)
        self.assertIn("接口异常", response.text)

    @patch("server.ai.ai_client.requests.post")
    def test_insufficient_system_resource_is_network_failure(self, post):
        post.return_value = _Resp(self.ok_choices | {
            "choices": [{"message": {"role": "assistant", "content": ""},
                         "finish_reason": "insufficient_system_resource"}]
        })
        response = call_ai("问题", self.model)
        self.assertFalse(response.success)
        self.assertTrue(is_network_failure(response.text))

    @patch("server.ai.ai_client.requests.post")
    def test_content_filter_marker(self, post):
        post.return_value = _Resp(self.ok_choices | {
            "choices": [{"message": {"role": "assistant", "content": "部分"},
                         "finish_reason": "content_filter"}]
        })
        response = call_ai("问题", self.model)
        self.assertFalse(response.success)
        self.assertIn(OUTPUT_FILTERED_MARKER, response.text)

    @patch("server.ai.ai_client.requests.post")
    def test_truncated_marker(self, post):
        post.return_value = _Resp(self.ok_choices | {
            "choices": [{"message": {"role": "assistant", "content": "{"},
                         "finish_reason": "length"}]
        })
        response = call_ai("问题", self.model)
        self.assertFalse(response.success)
        self.assertIn(OUTPUT_TRUNCATED_MARKER, response.text)


class _Resp:
    def __init__(self, data):
        self.data = data
        self.status_code = 200
        self.text = ""

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class TransientHttpHelperTests(unittest.TestCase):
    def test_transient_http_error_true_for_5xx_and_429_408(self):
        for code in (408, 429, 500, 503):
            self.assertTrue(
                ai_client._transient_http_error(_Err(code)),
                f"应视为瞬时: {code}",
            )

    def test_transient_http_error_false_for_stable_4xx(self):
        for code in (400, 401, 403, 404):
            self.assertFalse(ai_client._transient_http_error(_Err(code)))

    def test_transient_http_error_false_without_response(self):
        self.assertFalse(ai_client._transient_http_error(_Err(None)))


class _Err:
    def __init__(self, code):
        self.response = None if code is None else object()
        self.response = _FakeHTTPResponse(code) if code is not None else None


if __name__ == "__main__":
    unittest.main()
