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
    "daily_summary_retry_limit": 2,
    "weekly_report_retry_limit": 2,
    "monthly_report_retry_limit": 2,
    "empty_response_retry_limit": 1,
    "transient_http_retry_limit": 2,
    "transient_http_backoff_seconds": 1,
}
_POSITIVE_RETRY_SETTINGS = {
    "transient_http_backoff_seconds",
}
_MAXIMUM_RETRY_SETTINGS = {
    "empty_response_retry_limit": 1,
}


def is_valid_http_url(value: object) -> bool:
    text = str(value or "").strip()
    if re.search(r"[\x00-\x20\x7f]", text):
        return False
    try:
        parts = urlsplit(text)
        hostname = parts.hostname
    except ValueError:
        return False
    return parts.scheme.casefold() in {"http", "https"} and bool(hostname)


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
        value = yaml.safe_load(file)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RuntimeError("config.yaml 顶层必须是对象")
    return value


CONFIG = _load_config()
CONFIG_DIR = _get_config_path().parent


def _configured_path(key: str, default: str) -> Path:
    value = CONFIG.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"config.yaml 中 {key} 必须是非空路径字符串")
    path = Path(value)
    return path if path.is_absolute() else CONFIG_DIR / path


DIARY_DIR = _configured_path("diary_dir", "./Records")
ANALYSIS_DIR = _configured_path("analysis_dir", "./AnalysisReports")
LOG_DIR = _configured_path("log_dir", "./Log")
if DIARY_DIR.resolve() == ANALYSIS_DIR.resolve():
    raise RuntimeError("config.yaml 中 diary_dir 与 analysis_dir 不能相同")
DIARY_DIR.mkdir(parents=True, exist_ok=True)


def retry_policy() -> dict[str, int]:
    """Return the validated retry controls from ``config.yaml``."""
    configured = CONFIG.get("retry", {})
    if not isinstance(configured, dict):
        raise RuntimeError("config.yaml 中 retry 必须是对象")
    unknown = sorted(set(configured) - set(_RETRY_DEFAULTS))
    if unknown:
        raise RuntimeError(
            "config.yaml 中 retry 包含不支持的配置项: " + ", ".join(unknown)
        )
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
        models = CONFIG.get("models", [])
        if not isinstance(models, list):
            raise RuntimeError("config.yaml 中 models 必须是数组")
        for index, model in enumerate(models, 1):
            if not isinstance(model, dict):
                raise RuntimeError(f"config.yaml 中 models 第 {index} 项必须是对象")
            if not isinstance(model.get("name"), str) or not model["name"].strip():
                raise RuntimeError(
                    f"config.yaml 中 models 第 {index} 项必须包含非空 name"
                )
            if "api_url" in model and not isinstance(model["api_url"], str):
                raise RuntimeError(
                    f"config.yaml 中 models 第 {index} 项 api_url 必须是字符串"
                )
            if "model_id" in model and not isinstance(model["model_id"], str):
                raise RuntimeError(
                    f"config.yaml 中 models 第 {index} 项 model_id 必须是字符串"
                )
            if model.get("api_key") is not None and not isinstance(
                model["api_key"], str
            ):
                raise RuntimeError(
                    f"config.yaml 中 models 第 {index} 项 api_key 必须是字符串"
                )
            if "json_mode" in model and not isinstance(model["json_mode"], bool):
                raise RuntimeError(
                    f"config.yaml 中 models 第 {index} 项 json_mode 必须是布尔值"
                )
        return models

    @classmethod
    def get_model(cls, name_or_index: str | int | None = None) -> ModelDict:
        models = cls.models()
        if not models:
            raise RuntimeError("config.yaml 中未配置任何模型")
        if name_or_index is None:
            configured_name = CONFIG.get("current_model")
            if configured_name is None or configured_name == "":
                return dict(models[0])
            if not isinstance(configured_name, str):
                raise RuntimeError("config.yaml 中 current_model 必须是字符串")
            name_or_index = configured_name
        if isinstance(name_or_index, bool):
            raise TypeError("模型索引不能是布尔值")
        if isinstance(name_or_index, int):
            return dict(models[name_or_index % len(models)])
        if not isinstance(name_or_index, str):
            raise RuntimeError("config.yaml 中 current_model 必须是字符串")

        name_lower = name_or_index.lower()
        for model in models:
            if model["name"].lower() == name_lower:
                return dict(model)
        for model in models:
            if name_lower in model["name"].lower():
                return dict(model)
        raise KeyError(f"未找到匹配模型 '{name_or_index}'")

    @classmethod
    def next_after(cls, name: str) -> ModelDict:
        models = cls.models()
        if not models:
            raise RuntimeError("config.yaml 中未配置任何模型")
        index = next(
            (index for index, model in enumerate(models) if model["name"] == name),
            0,
        )
        return dict(models[(index + 1) % len(models)])

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
        try:
            temp_path.write_text(updated, encoding="utf-8")
            temp_path.replace(config_path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        CONFIG["current_model"] = model["name"]
        return model


def configuration_warnings() -> list[str]:
    """Return actionable configuration warnings without exposing secrets."""
    warnings = []
    try:
        models = ModelConfig.models()
    except RuntimeError as error:
        warnings.append(str(error))
        models = []
        models_valid = False
    else:
        models_valid = True
    for raw_model in models:
        try:
            hostname = urlsplit(str(raw_model.get("api_url", ""))).hostname or ""
        except ValueError:
            hostname = ""
        if hostname.casefold() == "api.deepseek.com" and "json_mode" not in raw_model:
            warnings.append(
                f"模型 {raw_model['name']} 缺少 json_mode；"
                "请在 config.yaml 中显式声明是否启用 JSON Output。"
            )
    if models_valid:
        try:
            active_model = ModelConfig.get_model()
        except (KeyError, RuntimeError, TypeError) as error:
            warnings.append(f"活动模型配置无效：{error}")
        else:
            api_url = str(active_model.get("api_url") or "").strip()
            if not is_valid_http_url(api_url):
                detail = "为空" if not api_url else "无效"
                warnings.append(
                    f"活动模型 {active_model.get('name', '未命名')} 的 api_url {detail}，"
                    "总结和报告暂时无法生成。"
                )
            if not str(active_model.get("api_key") or "").strip():
                warnings.append(
                    f"活动模型 {active_model.get('name', '未命名')} 的 api_key 为空，"
                    "总结和报告暂时无法生成。"
                )
    automation = CONFIG.get("automation", {})
    if not isinstance(automation, dict):
        warnings.append("config.yaml 中 automation 必须是对象")
        automation = {}
    else:
        for key in ("enabled", "daily_summary", "weekly_report", "monthly_report"):
            if key in automation and not isinstance(automation[key], bool):
                warnings.append(f"config.yaml 中 automation.{key} 必须是布尔值")
    try:
        retry_policy()
    except RuntimeError as error:
        warnings.append(str(error))
    return warnings
