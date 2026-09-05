"""客户端本地身份与设备状态：凭据 + 以时间戳为唯一标识。

credentials.json 保存服务端签发的**链接凭证 token**（不入中枢、不入数据空间）。
凭证不绑定设备：只要持有有效 token，任何客户端都能链接服务端同步。设备身份由
客户端自报本机名（device_name）区分，每条记录都会带上这个设备名。

entry_id 就是**写入的毫秒级时间戳字符串**（epoch ms）：`entry_id == str(ts)`。
去重、删除、排序都直接按时间戳比对：同一毫秒即同一条记录。时间戳本身不含时区
（epoch 是 UTC 绝对时间），展示时固定按 UTC+8 换算成分钟级 `HH:MM`。
"""

import json
import socket
import sys
from pathlib import Path

from .atomic_write import atomic_write


def credentials_path() -> Path:
    """返回客户端 credentials.json 路径。

    PyInstaller 单文件 exe 运行时 __file__ 指向临时 _MEIPASS（退出即删）；
    为保证凭据可见、可改且不被丢弃，打包后优先读 exe 同级目录的 credentials.json。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "credentials.json"
    return Path(__file__).resolve().parent / "credentials.json"


def _hostname() -> str:
    """读取本机名（如 Windows 的电脑名 MK8、手机默认名 vivo y78）。"""
    try:
        name = socket.gethostname()
    except OSError:
        name = ""
    return (name or "").strip() or "device"


def device_name() -> str:
    """设备名：直接用本机名（不允许在配置里自定义，保持各端自报本机名）。"""
    return _hostname()


def load() -> dict:
    path = credentials_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict) or not value.get("token"):
        return {}
    return {"token": value["token"]}


def save(token: str) -> None:
    atomic_write(credentials_path(), json.dumps({"token": token}, ensure_ascii=False, indent=2))


def require() -> dict:
    value = load()
    if not value:
        raise RuntimeError(
            "客户端未配置凭据：请先用服务端签发的 token 写入 credentials.json"
            "（格式见 credentials.example.json），仅可本地记录、无法同步。"
        )
    return value


def make_entry_id(ts: int) -> str:
    """条目标识 = 写入毫秒时间戳字符串（时间戳即 id）。

    去重 / 删除都直接按时间戳比对：同一写入毫秒即同一条，离线重推不会重复入库。
    """
    return str(int(ts))