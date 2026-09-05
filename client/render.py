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


def render_day_file(
    date: str,
    entries: list[dict],
    tombstones: list[dict] | None = None,
    summary: str = "",
) -> str:
    """把某日的条目与 tombstone 渲染成完整文件文本（按时间排序、墓碑插回原位）。

    与服务端 `server/hub/render.py` 的 render_day_file 逻辑保持一致（各自独立维护），
    使客户端本地镜像与服务端渲染的每日文件严格一致：墓碑占位按「原条目时间」插回
    记录流的原位置，而不是堆到末尾。
    """
    placed = []
    for entry in entries:
        placed.append((int(entry.get("ts", 0)), entry["entry_id"], entry_block(entry)))
    for tombstone in tombstones or []:
        # 旧 tombstone 无 entry_ts：回退到删除时间 ts（通常晚于条目），仍有确定顺序。
        sort_ts = int(tombstone.get("entry_ts", tombstone.get("ts", 0)))
        placed.append(
            (sort_ts, tombstone["entry_id"], tombstone_block(tombstone["entry_id"]))
        )
    placed.sort(key=lambda item: (item[0], item[1]))
    blocks = [item[2] for item in placed]
    body = "".join(block + "\n" for block in blocks) if blocks else "（当日暂无记录）\n"
    return day_header(date, summary) + body
