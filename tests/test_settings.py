import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from AgentRecord import settings


class ModelSettingsTests(unittest.TestCase):
    def test_source_config_remains_at_project_root(self):
        self.assertEqual(
            Path(settings.__file__).resolve().parent.parent / "config.yaml",
            settings._get_config_path(),
        )

    def test_config_loader_rejects_non_object_root(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text("[]\n", encoding="utf-8")
            with patch.object(
                settings, "_get_config_path", return_value=config_path
            ), self.assertRaisesRegex(RuntimeError, "顶层必须是对象"):
                settings._load_config()

    def test_log_directory_uses_config_relative_default(self):
        self.assertEqual(settings.CONFIG_DIR / "Log", settings.LOG_DIR)

    def test_selected_model_is_persisted_without_rewriting_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                "# 保留这条注释\n"
                "current_model: first\n"
                "models:\n"
                "  - name: first\n"
                "  - name: second\n",
                encoding="utf-8",
            )
            original_config = settings.CONFIG
            settings.CONFIG = {
                "current_model": "first",
                "models": [{"name": "first"}, {"name": "second"}],
            }
            try:
                with patch("AgentRecord.settings._get_config_path", return_value=config_path):
                    selected = settings.ModelConfig.select("second")
            finally:
                settings.CONFIG = original_config

            self.assertEqual("second", selected["name"])
            content = config_path.read_text(encoding="utf-8")
            self.assertIn("# 保留这条注释", content)
            self.assertIn("current_model: \"second\"", content)

    def test_deepseek_json_mode_must_be_explicit(self):
        raw_model = {
            "name": "deepseek",
            "api_url": "https://api.deepseek.com/chat/completions",
        }
        with patch.object(
            settings,
            "CONFIG",
            {"current_model": "deepseek", "models": [raw_model]},
        ):
            effective = settings.ModelConfig.get_model()

        self.assertNotIn("json_mode", effective)
        self.assertNotIn("max_tokens", effective)
        self.assertNotIn("json_mode", raw_model)

    def test_config_warnings_explain_provider_defaults_and_search_cap(self):
        with patch.object(
            settings,
            "CONFIG",
            {
                "models": [
                    {
                        "name": "deepseek",
                        "api_url": "https://api.deepseek.com/chat/completions",
                    }
                ],
                "third_search": {"count": 20},
            },
        ):
            warnings = settings.configuration_warnings()

        message = " ".join(warnings)
        self.assertNotIn("版本", message)
        self.assertIn("json_mode", message)
        self.assertIn("有效范围 1..10", message)

    def test_config_warnings_cover_active_key_and_automatic_weekly_search(self):
        with patch.object(
            settings,
            "CONFIG",
            {
                "current_model": "deepseek",
                "models": [
                    {
                        "name": "deepseek",
                        "api_url": "https://api.deepseek.com/chat/completions",
                        "api_key": None,
                    }
                ],
                "automation": {"enabled": True, "weekly_report": True},
                "third_search": {"enabled": False},
            },
        ):
            message = " ".join(settings.configuration_warnings())

        self.assertIn("api_key 为空", message)
        self.assertIn("自动周报已启用", message)

    def test_retry_policy_uses_configured_values_and_defaults(self):
        with patch.object(
            settings,
            "CONFIG",
            {"retry": {"agent_revision_limit": 0}},
        ):
            policy = settings.retry_policy()

        self.assertEqual(0, policy["agent_revision_limit"])
        self.assertEqual(2, policy["daily_summary_retry_limit"])
        self.assertEqual(2, policy["transient_http_retry_limit"])
        self.assertEqual(60, policy["automation_content_retry_interval_minutes"])

    def test_retry_policy_rejects_invalid_numeric_controls(self):
        with patch.object(
            settings,
            "CONFIG",
            {"retry": {"automation_content_failure_limit": 0}},
        ):
            with self.assertRaisesRegex(RuntimeError, "必须是正整数"):
                settings.retry_policy()

        with patch.object(
            settings,
            "CONFIG",
            {"retry": {"agent_revision_limit": 2}},
        ):
            with self.assertRaisesRegex(RuntimeError, "不能大于 1"):
                settings.retry_policy()

        with patch.object(
            settings,
            "CONFIG",
            {"retry": {"daily_summary_retry_limit": 3}},
        ):
            with self.assertRaisesRegex(RuntimeError, "不能大于 2"):
                settings.retry_policy()

        with patch.object(
            settings,
            "CONFIG",
            {"retry": {"unknown_retry": 1}},
        ):
            with self.assertRaisesRegex(RuntimeError, "不支持的配置项"):
                settings.retry_policy()

    def test_configuration_warnings_handle_malformed_sections(self):
        with patch.object(
            settings,
            "CONFIG",
            {"models": "bad", "third_search": "bad"},
        ):
            message = " ".join(settings.configuration_warnings())

        self.assertIn("models 必须是数组", message)
        self.assertIn("third_search 必须是对象", message)

    def test_configuration_warnings_reject_invalid_urls_and_timeout(self):
        with patch.object(
            settings,
            "CONFIG",
            {
                "current_model": "bad-url",
                "models": [
                    {
                        "name": "bad-url",
                        "api_url": "http://[",
                        "api_key": "configured",
                    }
                ],
                "third_search": {"timeout": "slow"},
            },
        ):
            message = " ".join(settings.configuration_warnings())

        self.assertIn("api_url 无效", message)
        self.assertIn("third_search.timeout 必须是正数", message)


if __name__ == "__main__":
    unittest.main()
