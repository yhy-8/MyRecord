"""原始日记的读取、追加、查找和总结区域更新。

每天一个 Markdown 文件及其内容格式由本模块统一维护。分析代码只能通过
这里提供的接口写入日记，避免未来 Agent 直接改动原始记录流。
"""

import datetime
import hashlib
import os
import re
import uuid
from pathlib import Path

from . import settings
from .file_lock import FileLock


RECORD_MARKER = "<!-- myrecord-record -->"
ESCAPED_RECORD_MARKER = "<!-- myrecord-record-text -->"


def _dated_diary_files() -> list[Path]:
    """Return only canonical ``YYYY-MM-DD.md`` diary files."""
    files = []
    for path in settings.DIARY_DIR.glob("*.md"):
        try:
            parsed = datetime.date.fromisoformat(path.stem)
        except ValueError:
            continue
        if parsed.isoformat() == path.stem:
            files.append(path)
    return files


def _acquire_journal_lock() -> FileLock:
    lock = FileLock.acquire(settings.DIARY_DIR / ".journal.lock", blocking=True)
    if lock is None:
        raise RuntimeError("日记文件锁获取失败")
    return lock


def resolve_date(arg: str) -> str:
    """解析常用日期参数，返回 YYYY-MM-DD，无法解析时返回空字符串。"""
    today = datetime.date.today()
    arg = arg.strip()

    if not arg:
        return today.strftime("%Y-%m-%d")

    if re.match(r"^-\d+$", arg):
        days = int(arg[1:])
        return (today - datetime.timedelta(days=days)).strftime("%Y-%m-%d")

    aliases = {"today": 0, "今天": 0, "yesterday": 1, "昨天": 1}
    if arg.lower() in aliases:
        return (today - datetime.timedelta(days=aliases[arg.lower()])).strftime("%Y-%m-%d")

    if arg.lower() in ("last", "prev", "上一个", "最近"):
        files = sorted(_dated_diary_files(), reverse=True)
        today_text = today.strftime("%Y-%m-%d")
        for file in files:
            if file.stem < today_text:
                return file.stem
        return ""

    for date_format in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.datetime.strptime(arg, date_format).strftime("%Y-%m-%d")
        except ValueError:
            continue

    short_date = re.fullmatch(r"(\d{1,2})-(\d{1,2})", arg)
    if short_date is None:
        short_date = re.fullmatch(r"(\d{2})(\d{2})", arg)
    if short_date:
        try:
            return datetime.date(
                today.year,
                int(short_date.group(1)),
                int(short_date.group(2)),
            ).isoformat()
        except ValueError:
            return ""
    return ""


def extract_summary(text: str) -> str:
    match = re.search(r"<summary>(.*?)</summary>", text, re.DOTALL)
    return match.group(1).strip() if match else "(无总结)"


def _diary_file_for(submitted_at: datetime.datetime) -> Path:
    return settings.DIARY_DIR / f"{submitted_at:%Y-%m-%d}.md"


def get_today_file() -> Path:
    return _diary_file_for(datetime.datetime.now())


def init_file_if_not_exists(
    submitted_at: datetime.datetime | None = None,
) -> Path:
    """使用同一个提交时间确定文件路径和文件头，并返回该路径。"""
    submitted_at = submitted_at or datetime.datetime.now()
    file_path = _diary_file_for(submitted_at)
    if file_path.exists():
        return file_path
    template = (
        f"# {submitted_at:%Y-%m-%d}\n\n"
        "<summary>\n暂无今日总结。\n</summary>\n\n"
        "---\n"
        "## 原始记录流\n\n"
    )
    file_path.write_text(template, encoding="utf-8")
    return file_path


def append_log(
    content: str,
    tag: str = "",
    submitted_at: datetime.datetime | None = None,
) -> None:
    """按回车提交时间追加记录；一次写入只使用一个时间值。"""
    submitted_at = submitted_at or datetime.datetime.now()
    content = re.sub(
        rf"^{re.escape(RECORD_MARKER)}\s*$",
        ESCAPED_RECORD_MARKER,
        content,
        flags=re.MULTILINE,
    )
    lock = _acquire_journal_lock()
    try:
        file_path = init_file_if_not_exists(submitted_at)
        submitted_time = submitted_at.strftime("%H:%M")
        with file_path.open("a", encoding="utf-8") as file:
            if tag:
                file.write(
                    f"{RECORD_MARKER}\n"
                    f"**{submitted_time} {tag}:** {content}\n\n"
                )
            else:
                file.write(
                    f"{RECORD_MARKER}\n**{submitted_time}:** {content}\n\n"
                )
    finally:
        lock.release()


def list_reference_sources(
    date_filter: str = "", limit: int = 20
) -> list[tuple[str, Path]]:
    """列出可引用的日记，按文件名倒序返回。"""
    files = sorted(_dated_diary_files(), reverse=True)
    if date_filter:
        files = [path for path in files if path.stem.startswith(date_filter)]
    if limit > 0:
        files = files[:limit]

    sources = []
    for path in files:
        period = path.stem
        sources.append((f"日记 | {period}", path))
    return sources


def append_reference(
    label: str,
    source_path: Path,
    note: str = "",
    submitted_at: datetime.datetime | None = None,
) -> None:
    """把来源及可选的新想法作为一条带时间的标准引用记录追加到今日日记。"""
    submitted_at = submitted_at or datetime.datetime.now()
    diary_path = _diary_file_for(submitted_at)
    relative_path = os.path.relpath(source_path, diary_path.parent)
    portable_path = Path(relative_path).as_posix()
    content = f"[{label}](<{portable_path}>)"
    if note.strip():
        content += f"\n\n{note.strip()}"
    append_log(content, "[引用]", submitted_at=submitted_at)


def update_summary_for_date(
    date: str,
    summary_text: str,
    *,
    expected_content_hash: str | None = None,
) -> str:
    file_path = settings.DIARY_DIR / f"{date}.md"
    lock = _acquire_journal_lock()
    try:
        if not file_path.exists():
            return f"找不到 {date} 的记录。"
        content = file_path.read_text(encoding="utf-8")
        if expected_content_hash is not None:
            actual_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if actual_hash != expected_content_hash:
                return f"{date} 的记录在总结生成期间发生变化，未写入过时总结。"
        if not re.search(r"<summary>.*?</summary>", content, re.DOTALL):
            return f"{date} 的记录缺少 <summary> 区域。"

        new_content = re.sub(
            r"<summary>.*?</summary>",
            lambda _match: f"<summary>\n{summary_text}\n</summary>",
            content,
            count=1,
            flags=re.DOTALL,
        )
        temp_path = file_path.with_suffix(
            file_path.suffix + f".{uuid.uuid4().hex}.tmp"
        )
        try:
            temp_path.write_text(new_content, encoding="utf-8")
            temp_path.replace(file_path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return f"{date} 的总结已写入文档顶部。"
    finally:
        lock.release()


def delete_last_record() -> bool:
    file_path = get_today_file()
    lock = _acquire_journal_lock()
    try:
        if not file_path.exists():
            return False
        content = file_path.read_text(encoding="utf-8")
        marker_matches = list(
            re.finditer(
                rf"^{re.escape(RECORD_MARKER)}\s*\n"
                r"(?=\*\*\d{2}:\d{2}(?: [^\n]*?)?:\*\*)",
                content,
                re.MULTILINE,
            )
        )
        header_matches = list(
            re.finditer(r"^\*\*\d{2}:\d{2}", content, re.MULTILINE)
        )
        if not header_matches:
            return False
        start = (
            marker_matches[-1].start()
            if marker_matches
            else header_matches[-1].start()
        )
        if start > 0 and content[start - 1] == "\n":
            start -= 1
        temp_path = file_path.with_suffix(
            file_path.suffix + f".{uuid.uuid4().hex}.tmp"
        )
        try:
            temp_path.write_text(
                content[:start].rstrip() + "\n\n", encoding="utf-8"
            )
            temp_path.replace(file_path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return True
    finally:
        lock.release()
