"""客户端配置：服务器地址、数据目录与轮询间隔。"""

import sys
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
    """返回客户端 config.yaml 路径。

    PyInstaller 单文件 exe 运行时，__file__ 指向临时解压目录 _MEIPASS，退出即被删除；
    若把配置/数据写在那里会全部丢失，且用户改不到 exe 旁的配置。
    因此打包后优先读 exe 同级目录的 config.yaml（build.yml 已把 client/config.yaml
    拷贝到 dist/ 即 exe 旁），保证配置可改、数据目录持久。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "config.yaml"
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