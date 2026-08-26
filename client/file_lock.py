"""跨进程互斥（基于 fcntl/flock）。Windows 打包环境由后台同步进程与交互进程
共用同一队列文件时使用同一锁，避免并发写坏文件。"""

import contextlib

try:
    import fcntl
except ImportError:  # Windows：退化为无锁（打包版为单进程模型）
    fcntl = None  # type: ignore


@contextlib.contextmanager
def file_lock(path):
    """阻塞式获取给定路径的排他锁，退出时释放。"""
    if fcntl is None:
        yield
        return
    with open(path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)