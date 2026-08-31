"""日记文件格式：渲染与解析（两端共用同一视觉格式与标记）。

每天仍是一个 `Records/YYYY-MM-DD.md` 容器。每条记录前带一个隐藏的
`<!-- myrecord-id:<id> -->` 标记，删除位置写
`<!-- myrecord-tombstone-id:<id> -->` 占位（不含正文）。这些标记在
Markdown 渲染中不可见，仅用于对账与去重。`<summary>` 区域由服务端独占写。

本模块同时负责把每日日记文件渲染成文件文本（render_*），以及把文件文本
解析回条目列表（parse_day_file，兼容旧 `agentrecord-*` 标记）。
"""

import datetime
import hashlib
import json
import re

ENTRY_MARKER_PREFIX = "<!-- myrecord-id:"
DEVICE_MARKER_PREFIX = "<!-- myrecord-device:"
TOMBSTONE_MARKER_PREFIX = "<!-- myrecord-tombstone-id:"

_DEFAULT_SUMMARY = "暂无今日总结。"


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


# ---------- 解析 ----------


_ENTRY_MARKER = re.compile(
    rf"^{re.escape(ENTRY_MARKER_PREFIX)}([^>]+) -->", re.MULTILINE
)
_DEVICE_MARKER = re.compile(
    rf"^{re.escape(DEVICE_MARKER_PREFIX)}([^>]+) -->", re.MULTILINE
)
_TOMBSTONE_MARKER = re.compile(
    rf"^{re.escape(TOMBSTONE_MARKER_PREFIX)}([^>]+) -->", re.MULTILINE
)
_RECORD = re.compile(
    r"^\*\*(\d{2}:\d{2})(?: ([^\n]*?))?:\*\*\s?(.*?)"
    r"(?=^\*\*\d{2}:\d{2}(?: [^\n]*?)?:\*\*|^\s*<!--|\Z)",
    re.MULTILINE | re.DOTALL,
)
# 记录正文在遇到下一条的标记行（任何 `<!-- ... -->` 注释，含新版 myrecord-* 与
# 旧版 agentrecord-*）或下一条时间行处截止，不会把标记注释吞进本条正文——
# AI 只应看到 `**HH:MM [设备名]:** 正文`。


def _ts_from(date: str, hhmm: str) -> int:
    hour, minute = (int(part) for part in hhmm.split(":"))
    year, month, day = (int(part) for part in date.split("-"))
    base = datetime.datetime(year, month, day, hour, minute)
    return int(base.timestamp())


def legacy_entry_id(date: str, index: int) -> str:
    payload = json.dumps(
        {"date": date, "index": index}, ensure_ascii=False, sort_keys=True
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]
    return f"legacy-{date.replace('-', '')}-{index:03d}-{digest}"


def parse_day_file(date: str, content: str) -> dict:
    """返回 {"entries": [...], "tombstones": [entry_id, ...]}。"""
    markers = [
        (m.start(), m.group(1)) for m in _ENTRY_MARKER.finditer(content)
    ]
    markers.sort(key=lambda item: item[0])
    dev_markers = [
        (m.start(), m.group(1)) for m in _DEVICE_MARKER.finditer(content)
    ]
    dev_markers.sort(key=lambda item: item[0])
    tombstones = [m.group(1) for m in _TOMBSTONE_MARKER.finditer(content)]
    records = list(_RECORD.finditer(content))

    entries = []
    marker_index = 0
    dev_index = 0
    for index, match in enumerate(records, 1):
        entry_id = None
        if marker_index < len(markers) and markers[marker_index][0] < match.start():
            entry_id = markers[marker_index][1]
            marker_index += 1
        if entry_id is None:
            entry_id = legacy_entry_id(date, index)
        device_id = ""
        if dev_index < len(dev_markers) and dev_markers[dev_index][0] < match.start():
            device_id = dev_markers[dev_index][1]
            dev_index += 1
        tag = (match.group(2) or "").strip()
        # 设备无标签渲染时，第 2 组就是 [设备名]，此时不当标签
        if device_id and tag == f"[{device_id}]":
            tag = ""
        text = match.group(3).strip()
        entries.append(
            {
                "entry_id": entry_id,
                "date": date,
                "device_id": device_id,
                "ts": _ts_from(date, match.group(1)),
                "tag": tag,
                "speaker": "quoted_ai" if "[AI回复]" in tag else "user",
                "text": text,
            }
        )
    return {"entries": entries, "tombstones": tombstones}