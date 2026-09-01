"""client/terminal.py 的跨平台输入（safe_input）在 POSIX 伪终端下的行为测试。

重点是验证 Linux 上中文退格删半字的修复：逐字符读取、按整字符退格。Windows 分支
（_safe_input_windows 走控制台事件）无法在无真实控制台的头测环境验证，且 CI 只跑
Windows，故此套用 `skipUnless` 仅在 POSIX 上运行。
"""

import os
import sys
import tempfile
import time
import unittest

try:
    import pty

    _HAVE_PTY = True
except ImportError:  # pragma: no cover
    _HAVE_PTY = False


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@unittest.skipUnless(
    _HAVE_PTY and sys.platform != "win32" and hasattr(os, "fork"),
    "仅在 POSIX 下用伪终端验证原生 raw 输入",
)
class TerminalSafeInputUnixTests(unittest.TestCase):
    """通过伪终端驱动真实 safe_input，模拟中文键入 + 退格。"""

    def _run_safe_input(self, chunks, timeout: float = 6.0) -> str:
        result_path = os.path.join(tempfile.mkdtemp(prefix="myrecord-input-"), "result.txt")
        ready_r, ready_w = os.pipe()
        pid, master = pty.fork()
        if pid == 0:  # 子进程：在伪终端里调用真实 safe_input
            try:
                # 先进入 raw，避免父进程在 canonical 期写入被行规程吞掉
                import termios
                import tty

                tty.setraw(0)
            except Exception:  # noqa: BLE001
                pass
            os.write(ready_w, b"R")
            os.close(ready_w)
            try:
                # pytest 会用捕获伪文件顶替 sys.stdin/out，重绑到真实 pty fd 0/1
                sys.stdin = os.fdopen(0, "r", encoding="utf-8")
                sys.stdout = os.fdopen(1, "w", encoding="utf-8")
                sys.path.insert(0, _repo_root())
                from client import terminal

                line = terminal.safe_input(">> ")
                with open(result_path, "w", encoding="utf-8") as handle:
                    handle.write(line)
            except BaseException as exc:  # noqa: BLE001
                with open(result_path, "w", encoding="utf-8") as handle:
                    handle.write(f"ERROR:{exc!r}")
            finally:
                os._exit(0)

        os.close(ready_w)
        try:
            os.read(ready_r, 1)  # 等子进程进入 raw 模式（先写 R）
            os.close(ready_r)
            time.sleep(0.3)  # 让子进程完全进入 safe_input 读循环
            os.write(master, b"".join(chunks))  # 一次写入，避免写入与时序竞争
            time.sleep(0.3)  # 让子进程读完并回显
        finally:
            os.close(master)

        # 有界等待子进程退出，避免测试挂起
        deadline = time.time() + timeout
        exited = False
        while time.time() < deadline:
            pid_out, _status = os.waitpid(pid, os.WNOHANG)
            if pid_out != 0:
                exited = True
                break
            time.sleep(0.05)
        if not exited:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
            self.fail("safe_input 子进程未在超时内退出")

        with open(result_path, encoding="utf-8") as handle:
            result = handle.read()
        if result.startswith("ERROR:"):
            self.fail(result)
        return result

    def test_backspace_deletes_whole_chinese_character(self):
        # 键入“你好”，退格，再键入“界”→ 退格删掉整字符“好”，得到“你界”
        result = self._run_safe_input(
            [
                "你".encode("utf-8"),
                "好".encode("utf-8"),
                b"\x7f",
                "界".encode("utf-8"),
                b"\r",
            ]
        )
        self.assertEqual("你界", result)

    def test_backspace_accepts_bs_byte_too(self):
        # 0x08（BS）与 0x7f（DEL）都应被当作退格
        result = self._run_safe_input(
            ["a".encode("utf-8"), b"\x08", "b".encode("utf-8"), b"\r"]
        )
        self.assertEqual("b", result)


if __name__ == "__main__":
    unittest.main()
