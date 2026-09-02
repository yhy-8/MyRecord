"""客户端本地日记：本地渲染 + 按天写入与对账。

每天一个 Records/YYYY-MM-DD.md 容器。每条记录前带一个隐藏的
`<!-- myrecord-id:<id> -->` 标记，删除位置写 tombstone 占位（不含正文）。
`<summary>` 区域由服务端独占写。本地写入永不因同步失败回滚；对账（apply_delta）
只做补齐与 tombstone 移除，不把已删条目推回。
"""

import datetime
import os
import re

from . import config
from .file_lock import file_lock

ENTRY_MARKER_PREFIX = "<!-- myrecord-id:"
DEVICE_MARKER_PREFIX = "<!-- myrecord-device:"
TOMBSTONE_MARKER_PREFIX = "<!-- myrecord-tombstone-id:"

_DEFAULT_SUMMARY = "暂无今日总结。"


# ---------- 渲染格式 ----------


def entry_block(entry: dict) -> str:
    tag = (entry.get("tag") or "").strip()
    dev = (entry.get("device_id") or "").strip()
    hhmm = datetime.datetime.fromtimestamp(int(entry.get("ts", 0))).strftime("%H:%M")
    entry_id = entry["entry_id"]
    if tag:
        header = f"**{hhmm} {tag}:** {entry.get('text', '')}"
    elif dev:
        header = f"**{hhmm} [{dev}]:** {entry.get('text', '')}"
    else:
        header = f"**{hhmm}:** {entry.get('text', '')}"
    line = f"{ENTRY_MARKER_PREFIX}{entry_id} -->\n"
    if dev:
        line += f"{DEVICE_MARKER_PREFIX}{dev} -->\n"
    return line + header + "\n"


def tombstone_block(entry_id: str) -> str:
    """只写一行 tombstone 占位（不含正文）。"""
    return f"{TOMBSTONE_MARKER_PREFIX}{entry_id} -->\n"


def day_header(date: str, summary: str = "") -> str:
    text = summary.strip() or _DEFAULT_SUMMARY
    return f"# {date}\n\n<summary>\n{text}\n</summary>\n\n---\n## 原始记录流\n\n"


# ---------- 本地写入与对账 ----------


def records_dir():
    return config.load()["client"]["records_dir"]


def day_path(date: str):
    return records_dir() / f"{date}.md"


def ensure_day_file(date: str) -> None:
    path = day_path(date)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(day_header(date), encoding="utf-8")


def append_record(entry: dict) -> None:
    """把一条新记录本地写入当天文件（原子追加，永不回滚）。"""
    date = entry["date"]
    # 与 apply_delta 共用同一把全局写锁：长轮询线程（apply_delta）与主输入线程
    # （append_record）可能并发写同一天文件，若各用不同锁文件会导致同一 entry_id
    # 被重复追加（本地 Records 出现重复块）。统一用 Records/.journal.lock 串行化。
    with file_lock(records_dir() / ".journal.lock"):
        ensure_day_file(date)
        path = day_path(date)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(entry_block(entry) + "\n")


def day_entry_ids(content: str) -> set[str]:
    return set(
        re.findall(
            r"^" + re.escape(ENTRY_MARKER_PREFIX) + r"([^>]+) -->",
            content,
            re.MULTILINE,
        )
    )


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
                body = "".join(entry_block(e) for e in missing)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(body + "\n")
        for tombstone in tombstones:
            _apply_tombstone(tombstone["entry_id"])


def _apply_tombstone(entry_id: str) -> None:
    prefix = re.escape(ENTRY_MARKER_PREFIX)
    dev_prefix = re.escape(DEVICE_MARKER_PREFIX)
    pattern = re.compile(
        rf"^{prefix}{re.escape(entry_id)} -->\n"
        rf"(?:{dev_prefix}[^\n]*\n)?"  # 可选：记录自带的设备名标记行
        r"[^\n]*\n",
        re.MULTILINE,
    )
    for path in records_dir().glob("*.md"):
        content = path.read_text(encoding="utf-8")
        match = pattern.search(content)
        if not match:
            continue
        rebuilt = (
            content[: match.start()]
            + tombstone_block(entry_id)
            + content[match.end():]
        )
        tmp = path.with_name(path.name + ".apply.tmp")
        tmp.write_text(rebuilt, encoding="utf-8")
        os.replace(tmp, path)