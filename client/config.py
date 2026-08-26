"""客户端配置：服务器地址、数据目录与轮询间隔。"""

from pathlib import Path

import yaml

_DEFAULTS = {
    "server_url": "http://localhost:8765",
    "records_dir": "./Records",
    "analysis_dir": "./AnalysisReports",
    "log_dir": "./Log",
    "poll_interval_seconds": 60,
    "longpoll_timeout_seconds": 25,
}


def config_path() -> Path:
    return Path(__file__).resolve().parent / "config.yaml"


def load() -> dict:
    path = config_path()
    raw = {}
    if path.exists():
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            value = {}
        if isinstance(value, dict) and isinstance(value.get("client"), dict):
            raw = value["client"]
    merged = dict(_DEFAULTS)
    for key in _DEFAULTS:
        if key in raw:
            merged[key] = raw[key]
    base = config_path().parent
    for key in ("records_dir", "analysis_dir", "log_dir"):
        p = Path(str(merged[key]))
        merged[key] = (base / p) if not p.is_absolute() else p
    return {"client": merged}