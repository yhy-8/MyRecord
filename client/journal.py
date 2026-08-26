"""客户端本地日记：每天一个 Records/YYYY-MM-DD.md 容器，本地渲染。

本地写入永不因同步失败回滚；对账（apply_delta）只做补齐与 tombstone 移除，
不把已删条目推回。
"""

import re

from . import config, render
from .file_lock import file_lock


def records_dir():
    return config.load()["client"]["records_dir"]


def day_path(date: str):
    return records_dir() / f"{date}.md"


def ensure_day_file(date: str) -> None:
    path = day_path(date)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(render.day_header(date), encoding="utf-8")


def append_record(entry: dict) -> None:
    """把一条新记录本地写入当天文件（原子追加，永不回滚）。"""
    date = entry["date"]
    with file_lock(day_path(date).with_suffix(".journal.lock")):
        ensure_day_file(date)
        path = day_path(date)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(render.entry_block(entry) + "\n")


def day_entry_ids(content: str) -> set[str]:
    return set(re.findall(r"^" + re.escape(render.ENTRY_MARKER_PREFIX) + r"([^>]+) -->",
                          content, re.MULTILINE))


def apply_delta(entries: list[dict], tombstones: list[dict]) -> None:
    """补齐缺失条目并按 tombstone 移除本地已删条目（本地渲染/对账）。"""
    by_date: dict[str, list[dict]] = {}
    for entry in entries:
        by_date.setdefault(entry["date"], []).append(entry)
    with file_lock(records_dir() / ".journal.lock"):
        for date, day_entries in by_date.items():
            ensure_day_file(date)
            path = day_path(date)
            content = path.read_text(encoding="utf-8")
            existing = day_entry_ids(content)
            missing = [e for e in day_entries if e["entry_id"] not in existing]
            if missing:
                body = "".join(render.entry_block(e) for e in missing)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(body + "\n")
        for tombstone in tombstones:
            _apply_tombstone(tombstone["entry_id"])


def _apply_tombstone(entry_id: str) -> None:
    prefix = re.escape(render.ENTRY_MARKER_PREFIX)
    pattern = re.compile(
        rf"^{prefix}{re.escape(entry_id)} -->\n" r"[^\n]*\n",
        re.MULTILINE,
    )
    for path in records_dir().glob("*.md"):
        content = path.read_text(encoding="utf-8")
        match = pattern.search(content)
        if not match:
            continue
        rebuilt = content[: match.start()] + render.tombstone_block(entry_id) + content[match.end():]
        tmp = path.with_name(path.name + ".apply.tmp")
        tmp.write_text(rebuilt, encoding="utf-8")
        import os
        os.replace(tmp, path)