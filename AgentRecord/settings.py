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
            effective.setdefault("max_tokens", 32768)
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
        missing = [
            key for key in ("json_mode", "max_tokens") if key not in raw_model
        ]
        if missing:
            warnings.append(
                f"模型 {raw_model.get('name', '未命名')} 缺少 "
                f"{', '.join(missing)}；本次已使用 DeepSeek 安全默认值，"
                "请更新实际运行目录的 config.yaml。"
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
    return warnings
