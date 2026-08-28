"""客户端日记渲染（与服务端同一视觉格式）。"""

import datetime

ENTRY_MARKER_PREFIX = "<!-- myrecord-entry:"
TOMBSTONE_MARKER_PREFIX = "<!-- myrecord-tombstone:"

_DEFAULT_SUMMARY = "暂无今日总结。"


def entry_block(entry: dict) -> str:
    tag = (entry.get("tag") or "").strip()
    hhmm = datetime.datetime.fromtimestamp(int(entry.get("ts", 0))).strftime("%H:%M")
    entry_id = entry["entry_id"]
    if tag:
        header = f"**{hhmm} {tag}:** {entry.get('text', '')}"
    else:
        header = f"**{hhmm}:** {entry.get('text', '')}"
    return f"{ENTRY_MARKER_PREFIX}{entry_id} -->\n{header}\n"


def tombstone_block(entry_id: str) -> str:
    return (
        f"{TOMBSTONE_MARKER_PREFIX}{entry_id} -->\n"
        f"> 此位置原有记录已删除。（entry_id: {entry_id}）\n"
    )


def day_header(date: str, summary: str = "") -> str:
    text = summary.strip() or _DEFAULT_SUMMARY
    return f"# {date}\n\n<summary>\n{text}\n</summary>\n\n---\n## 原始记录流\n\n"