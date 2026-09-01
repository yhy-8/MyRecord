"""服务端配置：监听、数据目录与（后续接入的）AI/搜索配置。"""

from pathlib import Path

import yaml

_DEFAULTS = {
    "host": "0.0.0.0",
    "port": 8765,
    "data_dir": "./data",
    "tls": {"certfile": "", "keyfile": ""},
}


def config_path() -> Path:
    return Path(__file__).resolve().parent / "config.yaml"


def _is_absolute_path(p: Path) -> bool:
    """Absolute as configured: has a drive, or is a POSIX-root path such as
    '/abs/data' (no drive) which Windows would otherwise treat as relative and
    re-base onto the config directory.  Such paths are honored unchanged."""
    return p.is_absolute() or (bool(p.root) and not p.drive)


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
    if _is_absolute_path(data_dir):
        merged["data_dir"] = data_dir
    else:
        merged["data_dir"] = (config_path().parent / data_dir).resolve()
    tls = merged.get("tls")
    if not isinstance(tls, dict):
        merged["tls"] = {"certfile": "", "keyfile": ""}
    else:
        merged["tls"] = {
            "certfile": str(tls.get("certfile") or ""),
            "keyfile": str(tls.get("keyfile") or ""),
        }
    base_dir = config_path().parent
    for key in ("certfile", "keyfile"):
        raw = merged["tls"][key]
        if raw and not _is_absolute_path(Path(raw)):
            merged["tls"][key] = str((base_dir / raw).resolve())
    # TLS 缺省指向本项目 `cert` 生成的证书（<data_dir>/tls/server.*），无需手动填路径；
    # 证书文件确实缺失时由 run 的强制 TLS 检查拦截（提示先执行 cert）。
    if not merged["tls"]["certfile"]:
        merged["tls"]["certfile"] = str(Path(merged["data_dir"]) / "tls" / "server.crt")
    if not merged["tls"]["keyfile"]:
        merged["tls"]["keyfile"] = str(Path(merged["data_dir"]) / "tls" / "server.key")
    return {"server": merged, "raw": value if isinstance(value, dict) else {}}
