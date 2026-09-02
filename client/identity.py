"""客户端本地身份与设备状态：凭据 + 设备无关的确定性条目编号。

credentials.json 保存服务端签发的**链接凭证 token**（不入中枢、不入数据空间）。
凭证不绑定设备：只要持有有效 token，任何客户端都能链接服务端同步。设备身份由
客户端自报本机名（device_name）区分，每条记录都会带上这个设备名。

entry_id 是**内容派生、设备无关**的确定性编号：
`sha256(date + ts + tag + text)` 截取前 16 位十六进制。任何客户端对同一条
记录（同一天、同一写入秒、同一正文、同一标签）都会算出同一个 entry_id，
因此离线多端各自记录、上线后合并时能被服务端按 entry_id 正确去重，
不会因生成端不同而产生重复条目。
"""

import hashlib
import json
import os
import socket
import sys
import uuid
from pathlib import Path

from .file_lock import file_lock


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
    path = credentials_path()
    payload = {"token": token}
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def require() -> dict:
    value = load()
    if not value:
        raise RuntimeError(
            "客户端未配置凭据：请先用服务端签发的 token 写入 credentials.json"
            "（格式见 credentials.example.json），仅可本地记录、无法同步。"
        )
    return value


def make_entry_id(date: str, ts: int, text: str, tag: str = "") -> str:
    """设备无关的确定性 entry_id：同一记录在任何客户端都得到同一个 id。

    输入为记录本身（日期、写入秒级时间戳、标签、正文），不含设备身份，
    因此离线多端各自生成后、上线合并时能被服务端按 id 正确去重。
    时间戳参与哈希，避免同一天重复写相同文字被误合并成一条。
    """
    payload = json.dumps(
        {"date": date, "ts": ts, "tag": tag, "text": text},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"e{digest[:16]}"