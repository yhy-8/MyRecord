"""日记文件渲染：两端共用同一视觉格式。

每天仍是一个 `Records/YYYY-MM-DD.md` 容器。每条记录前带一个隐藏的
`<!-- myrecord-entry:<entry_id> -->` 标记，删除位置写
`<!-- myrecord-tombstone:<entry_id> -->` 占位（不含正文）。这些标记在
Markdown 渲染中不可见，仅用于对账与去重。`<summary>` 区域由服务端独占写。
"""

import datetime

ENTRY_MARKER_PREFIX = "<!-- myrecord-entry:"
TOMBSTONE_MARKER_PREFIX = "<!-- myrecord-tombstone:"

_DEFAULT_SUMMARY = "暂无今日总结。"


def _fmt_hhmm(ts: int) -> str:
    dt = datetime.datetime.fromtimestamp(ts)
    return f"{dt:%H:%M}"


def entry_block(entry: dict) -> str:
    """把一条 entry 渲染成文件中的一块（隐藏标记 + 记录行）。"""
    tag = (entry.get("tag") or "").strip()
    hhmm = _fmt_hhmm(int(entry.get("ts", 0)))
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


def render_day_file(
    date: str,
    entries: list[dict],
    tombstones: list[dict] | None = None,
    summary: str = "",
) -> str:
    """把某日的条目与 tombstone 渲染成完整文件文本。"""
    ordered = sorted(entries, key=lambda e: (int(e.get("ts", 0)), e["entry_id"]))
    blocks = [entry_block(e) for e in ordered]
    for tombstone in tombstones or []:
        blocks.append(tombstone_block(tombstone["entry_id"]))
    body = "".join(blocks) if blocks else "（当日暂无记录）\n"
    return day_header(date, summary) + body + "\n"