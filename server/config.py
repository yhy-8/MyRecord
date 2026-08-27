"""服务端配置：监听、数据目录与（后续接入的）AI/搜索配置。"""

from pathlib import Path

import yaml

_DEFAULTS = {
    "host": "0.0.0.0",
    "port": 8765,
    "data_dir": "./data",
}


def config_path() -> Path:
    return Path(__file__).resolve().parent / "config.yaml"


def _merge(base: dict, extra: dict) -> dict:
    result = dict(base)
    result.update(extra or {})
    return result


def load() -> dict:
    path = config_path()
    server = {}
    value = {}
    if path.exists():
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            value = {}
        if isinstance(value, dict) and isinstance(value.get("server"), dict):
            server = value["server"]
    merged = _merge(_DEFAULTS, server)
    data_dir = Path(str(merged["data_dir"]))
    if not data_dir.is_absolute():
        data_dir = config_path().parent / data_dir
    merged["data_dir"] = data_dir.resolve()
    return {"server": merged, "raw": value if isinstance(value, dict) else {}}