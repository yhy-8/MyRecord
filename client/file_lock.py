"""跨进程互斥（基于 fcntl/flock）。Windows 打包环境由后台同步进程与交互进程
共用同一队列文件时使用同一锁，避免并发写坏文件。"""

import contextlib
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows：退化为无锁（打包版为单进程模型）
    fcntl = None  # type: ignore


@contextlib.contextmanager
def file_lock(path):
    """阻塞式获取给定路径的排他锁，退出时释放。

    首次使用时锁文件所在目录（如 Records/）可能尚不存在，这里先确保其父目录存在，
    否则打开锁文件会抛 FileNotFoundError。"""
    path = Path(path)
    if fcntl is None:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)