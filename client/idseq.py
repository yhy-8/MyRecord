"""每设备单调递增序号，用于生成 entry_id = device_id-<seq>（不依赖墙钟）。"""

import json
import os
import uuid
from pathlib import Path

from .file_lock import file_lock


def seq_path() -> Path:
    return Path(__file__).resolve().parent / "seq.json"


def _read() -> int:
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
        seq = _read() + 1
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