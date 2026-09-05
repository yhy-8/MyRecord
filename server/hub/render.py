"""日记文件格式：渲染与解析（服务端视角；客户端本地 `client/render.py` 为同款镜像，互不引用）。

每天仍是一个 `Records/YYYY-MM-DD.md` 容器。每条记录前带一个隐藏的
`<!-- myrecord-time:<毫秒时间戳> -->` 标记，删除位置写
`<!-- myrecord-tombstone-time:<时间戳> -->` 占位（不含正文）。这些标记在
Markdown 渲染中不可见，仅用于对账与去重。`<summary>` 区域由服务端独占写。

本模块同时负责把每日日记文件渲染成文件文本（render_*），以及把文件文本
解析回条目列表（parse_day_file）。
"""

import datetime
import hashlib
import json
import re

ENTRY_MARKER_PREFIX = "<!-- myrecord-time:"
DEVICE_MARKER_PREFIX = "<!-- myrecord-device:"
TOMBSTONE_MARKER_PREFIX = "<!-- myrecord-tombstone-time:"

# AI 写入日记时使用的可保留记录标记（与 time/device/tombstone 标记并存）。
RECORD_MARKER = "<!-- myrecord-record -->"
ESCAPED_RECORD_MARKER = "<!-- myrecord-record-text -->"

DEFAULT_SUMMARY = "暂无今日总结。"
_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)

# 展示/分组统一时区：epoch 是无时区的绝对时间，记录时间统一按 UTC+8 展示。
_UTC8 = datetime.timezone(datetime.timedelta(hours=8))


def _fmt_hhmm(ts: int) -> str:
    dt = datetime.datetime.fromtimestamp(ts / 1000.0, tz=_UTC8)
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


def extract_summary(text: str) -> str:
    """读取日记文本里的 <summary> 正文；缺失时返回占位符。"""
    match = _SUMMARY_RE.search(text)
    return match.group(1).strip() if match else "(无总结)"


def render_day_file(
    date: str,
    entries: list[dict],
    tombstones: list[dict] | None = None,
    summary: str = "",
) -> str:
    """把某日的条目与 tombstone 渲染成完整文件文本。

    墓碑（tombstone）占位按「原条目时间」插回记录流的原位置，与客户端本地
    apply_delta 的原位替换保持一致，而不是把所有墓碑堆到文件末尾：已删条目的
    占位落在它原来所在的时间位置，这样客户端与服务端渲染结果才严格一致。
    """
    placed = []
    for entry in entries:
        placed.append((int(entry.get("ts", 0)), entry["entry_id"], entry_block(entry)))
    for tombstone in tombstones or []:
        # 墓碑缺少原条目时间时回退到删除时间 ts（通常晚于条目），仍有确定顺序。
        sort_ts = int(tombstone.get("entry_ts", tombstone.get("ts", 0)))
        placed.append(
            (sort_ts, tombstone["entry_id"], tombstone_block(tombstone["entry_id"]))
        )
    placed.sort(key=lambda item: (item[0], item[1]))
    blocks = [item[2] for item in placed]
    # 每个条目/墓碑块后跟一个空行，与客户端本地 append_record 的逐块 + "\n" 格式一致；
    # 否则服务端渲染（或任何按整页重建）会把多条记录挤在一起、丢失换行。
    body = "".join(block + "\n" for block in blocks) if blocks else "（当日暂无记录）\n"
    return day_header(date, summary) + body


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
RECORD_PATTERN = re.compile(
    r"^\*\*(\d{2}:\d{2})(?: ([^\n]*?))?:\*\*\s?(.*?)"
    r"(?=^\*\*\d{2}:\d{2}(?: [^\n]*?)?:\*\*|^\s*<!--|\Z)",
    re.MULTILINE | re.DOTALL,
)
# 记录正文在遇到下一条的标记行（任何 `<!-- ... -->` 注释）或下一条时间行处截止，
# 不会把标记注释吞进本条正文——AI 只应看到 `**HH:MM [设备名]:** 正文`。


def _ts_from(date: str, hhmm: str) -> int:
    hour, minute = (int(part) for part in hhmm.split(":"))
    year, month, day = (int(part) for part in date.split("-"))
    # 展示时区为 UTC+8：解析回 epoch 时按 UTC+8 构造；
    # 显示时间只到分钟，以毫秒表达，保持与写入路径的 ts 单位一致
    base = datetime.datetime(year, month, day, hour, minute, tzinfo=_UTC8)
    return int(base.timestamp() * 1000)


def _entry_ts(entry_id: str, date: str, hhmm: str) -> int:
    """从条目标识推导毫秒时间戳。

    时间戳形态的 id（全数字）可直接还原精确子秒；无时间戳 id 的记录
    （如无标记的手写行）只能从显示 HH:MM 还原分钟对齐值。
    """
    if entry_id.isdigit():
        return int(entry_id)
    return _ts_from(date, hhmm)


def bare_entry_id(date: str, index: int) -> str:
    """无 id 标记的记录（裸 `**HH:MM:**` 行）按位置派生的确定性 id。"""
    payload = json.dumps(
        {"date": date, "index": index}, ensure_ascii=False, sort_keys=True
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]
    return f"bare-{date.replace('-', '')}-{index:03d}-{digest}"


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
    records = list(RECORD_PATTERN.finditer(content))

    entries = []
    marker_index = 0
    dev_index = 0
    for index, match in enumerate(records, 1):
        entry_id = None
        if marker_index < len(markers) and markers[marker_index][0] < match.start():
            entry_id = markers[marker_index][1]
            marker_index += 1
        if entry_id is None:
            entry_id = bare_entry_id(date, index)
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
                "ts": _entry_ts(entry_id, date, match.group(1)),
                "tag": tag,
                "speaker": "quoted_ai" if "[AI回复]" in tag else "user",
                "text": text,
            }
        )
    return {"entries": entries, "tombstones": tombstones}