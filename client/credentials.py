"""客户端本地凭据：{device_id, token}。不入中枢、不入数据空间。"""

import json
import os
import uuid
from pathlib import Path


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