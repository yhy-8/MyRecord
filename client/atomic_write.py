"""客户端原子文件写入：先写同目录临时文件再 os.replace 替换目标，避免写半截损坏文件。

客户端与服务端严格分离、各自独立部署，本文件是客户端自带的本地小工具；
服务端对应 `server/hub/atomic_write.py`（两处独立维护，逻辑保持一致，不互相引用）。
"""

import os
import uuid
from pathlib import Path


def atomic_write(path, text: str) -> None:
    """把 ``text`` 原子写入 ``path``。

    写入前确保目标父目录存在；临时文件带 UUID 后缀，替换后清理（出错时也清理）。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
