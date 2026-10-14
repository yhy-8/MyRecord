"""客户端本地身份与设备状态：凭据 + 以时间戳为唯一标识。

credentials.json 保存服务端签发的**链接凭证 token**（不入中枢、不入数据空间）。
凭证不绑定设备：只要持有有效 token，任何客户端都能链接服务端同步。设备身份由
客户端自报（默认本机名，可用 ``/name`` 自定义并持久化到 config.yaml）区分，每条记录
都会带上这个设备名。

entry_id 就是**写入的毫秒级时间戳字符串**（epoch ms）：`entry_id == str(ts)`。
去重、删除、排序都直接按时间戳比对：同一毫秒即同一条记录。时间戳本身不含时区
（epoch 是 UTC 绝对时间），展示时固定按 UTC+8 换算成分钟级 `HH:MM`。
"""

import json
import re
import socket
import sys
from pathlib import Path

from . import config
from .atomic_write import atomic_write


# 设备名会作为 HTTP 头 X-Device-Id 发送，必须是可编码为 latin-1 的单行 ASCII；
# 同时会渲染进日记的 `[设备名]` 标签，故只允许字母/数字/空格/._-。
_DEVICE_NAME_RE = re.compile(r"^[0-9A-Za-z._ -]{1,64}$")


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


def normalize_device_name(value: str) -> str | None:
    """把用户输入规范成合法设备名；不合法（含非 ASCII / 控制字符 / 过长）返回 None。"""
    name = (value or "").strip()
    return name if _DEVICE_NAME_RE.fullmatch(name) else None


def device_name() -> str:
    """设备名：优先用 ``/name`` 自定义并持久化在 config.yaml 的名字，否则用本机名。"""
    custom = config.load()["client"].get("device_name")
    name = str(custom).strip() if custom else ""
    return name or _hostname()


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