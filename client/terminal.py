"""跨平台终端输入：逐字符、Unicode 感知退格（Windows + POSIX）。
Windows 用控制台事件
（ReadConsoleInputW）读入并合并 UTF-16 surrogate；POSIX TTY 在 raw 模式下逐字节累积成
完整码点。二者退格都按【整字符】删除，避免 locale 未知时多字节字符被删掉半个字节、
留下坏字符被写入日记。
"""

import ctypes
import os
import queue
import select
import shutil
import sys
import time

from rich.cells import cell_len
from rich.console import Console


def _configure_utf8_stdio() -> None:
    """Keep Windows CI and legacy consoles from encoding Chinese as cp1252."""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue


_configure_utf8_stdio()


try:
    import msvcrt

    IS_WINDOWS = True

    kernel32 = ctypes.windll.kernel32
    kernel32.GetStdHandle.argtypes = [ctypes.c_int32]
    kernel32.GetStdHandle.restype = ctypes.c_void_p
    kernel32.GetConsoleMode.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    kernel32.GetConsoleMode.restype = ctypes.c_int32
    kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.SetConsoleMode.restype = ctypes.c_int32
    handle = kernel32.GetStdHandle(-11)
    mode = ctypes.c_uint32()
    if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
except ImportError:
    import termios
    import tty

    IS_WINDOWS = False


console = Console()
_NOTIFICATIONS: queue.SimpleQueue[tuple[str, str | None]] = queue.SimpleQueue()

_WINDOWS_NOTIFICATION_INTERVAL_MS = 50
_STD_INPUT_HANDLE = -10
_KEY_EVENT = 0x0001
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258


class _WindowsCharacter(ctypes.Union):
    _fields_ = [
        ("UnicodeChar", ctypes.c_uint16),
        ("AsciiChar", ctypes.c_char),
    ]


class _WindowsKeyEvent(ctypes.Structure):
    _fields_ = [
        ("KeyDown", ctypes.c_int32),
        ("RepeatCount", ctypes.c_uint16),
        ("VirtualKeyCode", ctypes.c_uint16),
        ("VirtualScanCode", ctypes.c_uint16),
        ("Character", _WindowsCharacter),
        ("ControlKeyState", ctypes.c_uint32),
    ]


class _WindowsInputEventData(ctypes.Union):
    _fields_ = [
        ("KeyEvent", _WindowsKeyEvent),
        ("Padding", ctypes.c_byte * 16),
    ]


class _WindowsInputEvent(ctypes.Structure):
    _fields_ = [
        ("EventType", ctypes.c_uint16),
        ("Event", _WindowsInputEventData),
    ]


def post_notification(message: str, style: str | None = None) -> None:
    """Queue a worker-thread notification for rendering by the input thread."""
    _NOTIFICATIONS.put((message, style))


def _pending_notifications() -> list[tuple[str, str | None]]:
    notifications = []
    while True:
        try:
            notifications.append(_NOTIFICATIONS.get_nowait())
        except queue.Empty:
            return notifications


def _start_input(prompt: str) -> None:
    sys.stdout.write(prompt)
    sys.stdout.flush()


def _wrapped_input_rows(prompt: str, characters: list[str], columns: int) -> int:
    """Return how many rows the live cursor moved below the input origin."""
    return cell_len(prompt + "".join(characters)) // max(1, columns)


def _clear_rendered_input(prompt: str, characters: list[str]) -> None:
    """Clear input relative to its live cursor so console scrolling is harmless."""
    columns = max(1, shutil.get_terminal_size().columns)
    wrapped_rows = _wrapped_input_rows(prompt, characters, columns)
    move_up = f"\x1b[{wrapped_rows}A" if wrapped_rows else ""
    sys.stdout.write(f"{move_up}\r\x1b[0J")


def _redraw_line(
    prompt: str, characters: list[str], removed_character: str
) -> None:
    _clear_rendered_input(prompt, [*characters, removed_character])
    sys.stdout.write(prompt + "".join(characters))
    sys.stdout.flush()


def _show_notifications(prompt: str, characters: list[str]) -> None:
    notifications = _pending_notifications()
    if not notifications:
        return
    _clear_rendered_input(prompt, characters)
    sys.stdout.flush()
    for message, style in notifications:
        console.print(message, style=style, markup=False)
    sys.stdout.write(prompt + "".join(characters))
    sys.stdout.flush()


def _windows_console_input_handle():
    try:
        handle = ctypes.windll.kernel32.GetStdHandle(_STD_INPUT_HANDLE)
        invalid = ctypes.c_void_p(-1).value
        return None if handle in (None, 0, invalid) else handle
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _read_windows_input_event(handle, timeout_ms: int) -> list[str] | None:
    """Consume one native console event; ``None`` means no event was ready."""
    kernel32 = ctypes.windll.kernel32
    try:
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.ReadConsoleInputW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_WindowsInputEvent),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        kernel32.ReadConsoleInputW.restype = ctypes.c_int32
    except (AttributeError, TypeError):
        pass
    wait_result = kernel32.WaitForSingleObject(
        ctypes.c_void_p(handle), timeout_ms
    )
    if wait_result == _WAIT_TIMEOUT:
        return None
    if wait_result != _WAIT_OBJECT_0:
        raise OSError("等待 Windows 控制台输入失败")

    event = _WindowsInputEvent()
    count = ctypes.c_uint32()
    if not kernel32.ReadConsoleInputW(
        ctypes.c_void_p(handle), ctypes.byref(event), 1, ctypes.byref(count)
    ):
        raise OSError("读取 Windows 控制台输入失败")
    if count.value != 1 or event.EventType != _KEY_EVENT:
        return []
    key = event.Event.KeyEvent
    if not key.KeyDown or not key.Character.UnicodeChar:
        return []
    character = chr(key.Character.UnicodeChar)
    return [character] * max(1, int(key.RepeatCount))


def _windows_character() -> str:
    """Read one Unicode character, joining a UTF-16 surrogate pair if needed."""
    character = msvcrt.getwch()
    codepoint = ord(character)
    if 0xD800 <= codepoint <= 0xDBFF:
        trailing = msvcrt.getwch()
        trailing_codepoint = ord(trailing)
        if 0xDC00 <= trailing_codepoint <= 0xDFFF:
            return chr(
                0x10000
                + ((codepoint - 0xD800) << 10)
                + (trailing_codepoint - 0xDC00)
            )
        return ""
    if 0xDC00 <= codepoint <= 0xDFFF:
        return ""
    return character


def _safe_input_unix(prompt: str) -> str:
    _start_input(prompt)
    file_descriptor = sys.stdin.fileno()
    old_settings = termios.tcgetattr(file_descriptor)

    try:
        tty.setraw(file_descriptor)
        characters: list[str] = []
        while True:
            if not select.select([file_descriptor], [], [], 0.1)[0]:
                _show_notifications(prompt, characters)
                continue
            byte = os.read(file_descriptor, 1)
            if not byte:
                break
            if byte == b"\r":
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                break
            if byte in (b"\x7f", b"\x08"):
                if characters:
                    removed_character = characters.pop()
                    _redraw_line(prompt, characters, removed_character)
                continue
            if byte == b"\x03":
                sys.stdout.write("^C\r\n")
                sys.stdout.flush()
                raise KeyboardInterrupt()
            if byte == b"\x04":
                if not characters:
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    raise EOFError()
                continue
            if byte == b"\x1b":
                while select.select([file_descriptor], [], [], 0.05)[0]:
                    os.read(file_descriptor, 16)
                continue
            if byte[0] < 0x20 or byte[0] == 0x7F:
                continue

            if byte[0] & 0x80 == 0:
                trailing_bytes = 0
            elif byte[0] & 0xE0 == 0xC0:
                trailing_bytes = 1
            elif byte[0] & 0xF0 == 0xE0:
                trailing_bytes = 2
            elif byte[0] & 0xF8 == 0xF0:
                trailing_bytes = 3
            else:
                continue

            character_bytes = byte
            for _ in range(trailing_bytes):
                character_bytes += os.read(file_descriptor, 1)
            try:
                character = character_bytes.decode("utf-8")
            except UnicodeDecodeError:
                continue
            characters.append(character)
            sys.stdout.write(character)
            sys.stdout.flush()
    finally:
        termios.tcsetattr(file_descriptor, termios.TCSADRAIN, old_settings)
    return "".join(characters)


def _safe_input_windows(prompt: str) -> str:
    _start_input(prompt)
    characters: list[str] = []
    pending_surrogate = ""
    input_handle = _windows_console_input_handle()

    while True:
        if input_handle is None:
            if not msvcrt.kbhit():
                _show_notifications(prompt, characters)
                time.sleep(_WINDOWS_NOTIFICATION_INTERVAL_MS / 1000)
                continue
            incoming = [_windows_character()]
        else:
            _show_notifications(prompt, characters)
            try:
                incoming = _read_windows_input_event(
                    input_handle, _WINDOWS_NOTIFICATION_INTERVAL_MS
                )
            except (AttributeError, OSError, TypeError, ValueError):
                input_handle = None
                continue
            if incoming is None:
                continue

        pending_echo: list[str] = []
        while incoming is not None:
            for character in incoming:
                if not character:
                    continue
                codepoint = ord(character)
                if 0xD800 <= codepoint <= 0xDBFF:
                    pending_surrogate = character
                    continue
                if 0xDC00 <= codepoint <= 0xDFFF:
                    if not pending_surrogate:
                        continue
                    leading_codepoint = ord(pending_surrogate)
                    character = chr(
                        0x10000
                        + ((leading_codepoint - 0xD800) << 10)
                        + (codepoint - 0xDC00)
                    )
                    pending_surrogate = ""
                elif pending_surrogate:
                    pending_surrogate = ""

                if character in ("\r", "\n"):
                    sys.stdout.write("".join(pending_echo) + "\r\n")
                    sys.stdout.flush()
                    return "".join(characters)
                if character == "\x08":
                    if pending_echo:
                        sys.stdout.write("".join(pending_echo))
                        sys.stdout.flush()
                        pending_echo.clear()
                    if characters:
                        removed_character = characters.pop()
                        _redraw_line(prompt, characters, removed_character)
                    continue
                if character == "\x03":
                    sys.stdout.write("".join(pending_echo) + "^C\r\n")
                    sys.stdout.flush()
                    raise KeyboardInterrupt()
                if character == "\x1a":
                    if not characters:
                        sys.stdout.write("^Z\r\n")
                        sys.stdout.flush()
                        raise EOFError()
                    continue
                if character in ("\x00", "\xe0"):
                    if input_handle is None:
                        msvcrt.getwch()
                    continue
                if character == "\x1b":
                    if pending_echo:
                        sys.stdout.write("".join(pending_echo))
                        sys.stdout.flush()
                        pending_echo.clear()
                    if input_handle is None:
                        time.sleep(0.03)
                        while msvcrt.kbhit():
                            msvcrt.getwch()
                    continue
                if ord(character) < 0x20 or ord(character) == 0x7F:
                    continue
                characters.append(character)
                pending_echo.append(character)

            if input_handle is None:
                incoming = (
                    [_windows_character()] if msvcrt.kbhit() else None
                )
            else:
                try:
                    incoming = _read_windows_input_event(input_handle, 0)
                except (AttributeError, OSError, TypeError, ValueError):
                    input_handle = None
                    incoming = None

        if pending_echo:
            sys.stdout.write("".join(pending_echo))
            sys.stdout.flush()


def safe_input(prompt: str = "") -> str:
    return _safe_input_windows(prompt) if IS_WINDOWS else _safe_input_unix(prompt)
