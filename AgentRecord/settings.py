"""配置、目录和模型选择。

该模块只负责运行配置，不包含日记、模型请求或分析业务。
"""

import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml


ModelDict = dict[str, Any]


_RETRY_DEFAULTS = {
    "agent_revision_limit": 1,
    "empty_response_retry_limit": 1,
    "transient_http_retry_limit": 2,
    "transient_http_backoff_seconds": 1,
    "automation_content_failure_limit": 2,
    "automation_network_retry_minutes": 5,
    "automation_content_retry_interval_minutes": 60,
}
_POSITIVE_RETRY_SETTINGS = {
    "transient_http_backoff_seconds",
    "automation_content_failure_limit",
    "automation_network_retry_minutes",
    "automation_content_retry_interval_minutes",
}
_MAXIMUM_RETRY_SETTINGS = {
    "agent_revision_limit": 1,
    "empty_response_retry_limit": 1,
}


def _get_config_path() -> Path:
    """获取 config.yaml 路径，兼容 PyInstaller 打包后的路径。"""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parent.parent
    return base / "config.yaml"


def _load_config() -> dict:
    config_path = _get_config_path()
    if not config_path.exists():
        return {}
    with config_path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


CONFIG = _load_config()
CONFIG_DIR = _get_config_path().parent


def _configured_path(key: str, default: str) -> Path:
    path = Path(CONFIG.get(key, default))
    return path if path.is_absolute() else CONFIG_DIR / path


DIARY_DIR = _configured_path("diary_dir", "./Records")
ANALYSIS_DIR = _configured_path("analysis_dir", "./AnalysisReports")
LOG_DIR = _configured_path("log_dir", "./Log")
DIARY_DIR.mkdir(parents=True, exist_ok=True)


def retry_policy() -> dict[str, int]:
    """Return the validated retry controls from ``config.yaml``."""
    configured = CONFIG.get("retry", {})
    if not isinstance(configured, dict):
        raise RuntimeError("config.yaml 中 retry 必须是对象")
    policy = {}
    for key, default in _RETRY_DEFAULTS.items():
        value = configured.get(key, default)
        minimum = 1 if key in _POSITIVE_RETRY_SETTINGS else 0
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            comparator = "正整数" if minimum else "非负整数"
            raise RuntimeError(f"config.yaml 中 retry.{key} 必须是{comparator}")
        maximum = _MAXIMUM_RETRY_SETTINGS.get(key)
        if maximum is not None and value > maximum:
            raise RuntimeError(
                f"config.yaml 中 retry.{key} 不能大于 {maximum}"
            )
        policy[key] = value
    return policy


class ModelConfig:
    """统一管理 OpenAI 兼容模型配置。"""

    @classmethod
    def models(cls) -> list[ModelDict]:
        return CONFIG.get("models", [])

    @staticmethod
    def _effective(model: ModelDict) -> ModelDict:
        """Apply narrow provider defaults so preserved old configs stay reliable."""
        effective = dict(model)
        hostname = urlsplit(str(effective.get("api_url", ""))).hostname or ""
        if hostname.casefold() == "api.deepseek.com":
            effective.setdefault("json_mode", True)
        return effective

    @classmethod
    def get_model(cls, name_or_index: str | int | None = None) -> ModelDict:
        models = cls.models()
        if not models:
            raise RuntimeError("config.yaml 中未配置任何模型")
        if name_or_index is None:
            name_or_index = CONFIG.get("current_model")
            if not name_or_index:
                return cls._effective(models[0])
        if isinstance(name_or_index, int):
            return cls._effective(models[name_or_index % len(models)])

        name_lower = name_or_index.lower()
        for model in models:
            if model["name"].lower() == name_lower:
                return cls._effective(model)
        for model in models:
            if name_lower in model["name"].lower():
                return cls._effective(model)
        raise KeyError(f"未找到匹配模型 '{name_or_index}'")

    @classmethod
    def index_of(cls, name: str) -> int:
        for index, model in enumerate(cls.models()):
            if model["name"] == name:
                return index
        return 0

    @classmethod
    def next_after(cls, name: str) -> ModelDict:
        models = cls.models()
        index = cls.index_of(name)
        return cls._effective(models[(index + 1) % len(models)])

    @classmethod
    def select(cls, name: str) -> ModelDict:
        """持久化统一模型选择，同时保留配置文件中的注释与原有排版。"""
        model = cls.get_model(name)
        config_path = _get_config_path()
        content = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
        selected_line = f"current_model: {json.dumps(model['name'], ensure_ascii=False)}"
        pattern = re.compile(r"^current_model\s*:.*$", re.MULTILINE)
        if pattern.search(content):
            updated = pattern.sub(selected_line, content, count=1)
        else:
            separator = "" if not content or content.endswith("\n") else "\n"
            updated = f"{content}{separator}{selected_line}\n"

        temp_path = config_path.with_suffix(
            config_path.suffix + f".{uuid.uuid4().hex}.tmp"
        )
        temp_path.write_text(updated, encoding="utf-8")
        temp_path.replace(config_path)
        CONFIG["current_model"] = model["name"]
        return model


def configuration_warnings() -> list[str]:
    """Return actionable configuration warnings without exposing secrets."""
    warnings = []
    for raw_model in ModelConfig.models():
        hostname = urlsplit(str(raw_model.get("api_url", ""))).hostname or ""
        if hostname.casefold() != "api.deepseek.com":
            continue
        missing = [key for key in ("json_mode",) if key not in raw_model]
        if missing:
            warnings.append(
                f"模型 {raw_model.get('name', '未命名')} 缺少 "
                f"{', '.join(missing)}；本次已使用 DeepSeek 安全默认值，"
                "请更新实际运行目录的 config.yaml。"
            )
    try:
        active_model = ModelConfig.get_model()
    except (KeyError, RuntimeError, TypeError) as error:
        warnings.append(f"活动模型配置无效：{error}")
    else:
        if not str(active_model.get("api_key", "")).strip():
            warnings.append(
                f"活动模型 {active_model.get('name', '未命名')} 的 api_key 为空，"
                "总结和报告暂时无法生成。"
            )
    third_search = CONFIG.get("third_search", {})
    try:
        count = int(third_search.get("count", 10))
    except (TypeError, ValueError):
        count = 10
    if count > 10:
        warnings.append(
            f"third_search.count={count} 超过有效上限 10；运行时会按 10 处理。"
        )
    automation = CONFIG.get("automation", {})
    weekly_automatic = bool(
        isinstance(automation, dict)
        and automation.get("enabled", False)
        and automation.get("weekly_report", False)
    )
    search_ready = bool(
        isinstance(third_search, dict)
        and third_search.get("enabled", False)
        and third_search.get("api_url", "")
        and third_search.get("api_key", "")
    )
    if weekly_automatic and not search_ready:
        warnings.append(
            "自动周报已启用，但 third_search 未启用或缺少 api_url/api_key；"
            "周报会在配置检查阶段暂停。"
        )
    try:
        retry_policy()
    except RuntimeError as error:
        warnings.append(str(error))
    return warnings
