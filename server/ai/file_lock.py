"""Small cross-process file lock used by report and automation writers."""

import os
from pathlib import Path


class FileLock:
    def __init__(self, path: Path, file_object):
        self.path = path
        self._file = file_object

    @classmethod
    def acquire(cls, path: Path, *, blocking: bool = False) -> "FileLock | None":
        path.parent.mkdir(parents=True, exist_ok=True)
        file_object = path.open("a+b")
        try:
            file_object.seek(0, os.SEEK_END)
            if file_object.tell() == 0:
                file_object.write(b"0")
                file_object.flush()
            file_object.seek(0)
            if os.name == "nt":
                import msvcrt

                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                msvcrt.locking(file_object.fileno(), mode, 1)
            else:
                import fcntl

                mode = fcntl.LOCK_EX
                if not blocking:
                    mode |= fcntl.LOCK_NB
                fcntl.flock(file_object.fileno(), mode)
        except (OSError, IOError):
            file_object.close()
            return None
        return cls(path, file_object)

    def release(self) -> None:
        if self._file.closed:
            return
        try:
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
