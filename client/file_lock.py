"""跨进程互斥（基于 fcntl/flock），仅 POSIX 生效。

客户端打包版（Windows/exe）为**单进程模型**：交互主线程与后台同步线程同在一进程内，
没有跨进程竞争，故 Windows 上退化为无锁（`fcntl` 不可用 → `file_lock` 为空操作）。
若未来启动多进程/多实例，需再引入 msvcrt 锁（参照服务端 `server/ai/file_lock.py`）。
"""

import contextlib
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows/Linux 之外的平台：无 fcntl → 无锁（单进程模型）
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