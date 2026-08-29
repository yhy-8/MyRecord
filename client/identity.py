"""客户端本地身份与设备状态：凭据 + 每设备单调序号。

credentials.json 保存服务端签发的 device_id/token（不入中枢、不入数据空间）；
seq.json 保存本设备单调递增序号，用于生成 entry_id = device_id-<seq>（不依赖墙钟）。
"""

import json
import os
import uuid
from pathlib import Path

from .file_lock import file_lock


def credentials_path() -> Path:
    return Path(__file__).resolve().parent / "credentials.json"


def load() -> dict:
    path = credentials_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict) or not value.get("device_id") or not value.get("token"):
        return {}
    return {"device_id": value["device_id"], "token": value["token"]}


def save(device_id: str, token: str) -> None:
    path = credentials_path()
    payload = json.dumps(
        {"device_id": device_id, "token": token}, ensure_ascii=False, indent=2
    )
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
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
            "客户端未配置凭据：请先登录（将服务端签发的 device_id 与 token 写入 "
            "credentials.json）。"
        )
    return value


def seq_path() -> Path:
    return Path(__file__).resolve().parent / "seq.json"


def _read_seq() -> int:
    path = seq_path()
    if not path.exists():
        return 0
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    return int(value.get("seq", 0) or 0) if isinstance(value, dict) else 0


def next_seq() -> int:
    """原子地取下一个序号并持久化。"""
    with file_lock(seq_path()):
        seq = _read_seq() + 1
        tmp = seq_path().with_name(seq_path().name + f".{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps({"seq": seq}), encoding="utf-8")
            os.replace(tmp, seq_path())
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return seq


def make_entry_id(device_id: str) -> str:
    return f"{device_id}-{next_seq()}"