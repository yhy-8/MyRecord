"""客户端配置：服务器地址、数据目录与轮询间隔。"""

from pathlib import Path

import yaml

_DEFAULTS = {
    "server_url": "https://localhost:8765",
    "records_dir": "../Records",
    "analysis_dir": "../AnalysisReports",
    "longpoll_timeout_seconds": 25,
    "verify": "",
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
    # 相对路径以 client/ 为基准（默认 ../Records、../AnalysisReports 指向 client 同级目录）。
    # resolve() 把 `..` 归一化为绝对路径，避免后续文件操作残留 `..`。
    base = config_path().parent
    for key in ("records_dir", "analysis_dir"):
        p = Path(str(merged[key]))
        merged[key] = ((base / p) if not p.is_absolute() else p).resolve()
    return {"client": merged}