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

    def test_deepseek_legacy_config_receives_narrow_structured_defaults(self):
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

        self.assertTrue(effective["json_mode"])
        self.assertEqual(32768, effective["max_tokens"])
        self.assertNotIn("json_mode", raw_model)

    def test_config_warnings_explain_legacy_defaults_and_search_cap(self):
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
        self.assertIn("当前版本为 2", message)
        self.assertIn("json_mode, max_tokens", message)
        self.assertIn("有效上限 10", message)


if __name__ == "__main__":
    unittest.main()
