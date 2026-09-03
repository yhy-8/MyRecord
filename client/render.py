"""日记文件格式：客户端本地渲染（标记常量 + 逐块/整页渲染）。

每天仍是一个 `Records/YYYY-MM-DD.md` 容器。每条记录前带一个隐藏的
`<!-- myrecord-id:<id> -->` 标记，删除位置写
`<!-- myrecord-tombstone-id:<id> -->` 占位（不含正文）。这些标记在
Markdown 渲染中不可见，仅用于对账与去重。`<summary>` 区域由服务端独占写。

客户端与服务端**严格分离、各自独立部署**：本文件是客户端自带的本地渲染
（只含客户端本地写入所需的最小渲染帮助），与 `server/hub/render.py` 独立维护、
逻辑保持一致，不互相引用；反向解析（parse_day_file 等）只存在于服务端。
"""

import datetime

ENTRY_MARKER_PREFIX = "<!-- myrecord-id:"
DEVICE_MARKER_PREFIX = "<!-- myrecord-device:"
TOMBSTONE_MARKER_PREFIX = "<!-- myrecord-tombstone-id:"

DEFAULT_SUMMARY = "暂无今日总结。"


def _fmt_hhmm(ts: int) -> str:
    dt = datetime.datetime.fromtimestamp(ts)
    return f"{dt:%H:%M}"


def entry_block(entry: dict) -> str:
    """把一条 entry 渲染成文件中的一块（隐藏标记 + 记录行）。"""
    tag = (entry.get("tag") or "").strip()
    dev = (entry.get("device_id") or "").strip()
    hhmm = _fmt_hhmm(int(entry.get("ts", 0)))
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
    text = summary.strip() or DEFAULT_SUMMARY
    return f"# {date}\n\n<summary>\n{text}\n</summary>\n\n---\n## 原始记录流\n\n"
