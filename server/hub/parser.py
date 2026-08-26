"""解析每日日记文件为条目列表（兼容新的 entry_id 标记与旧 RECORD_MARKER）。"""

import datetime
import hashlib
import json
import re

from .render import ENTRY_MARKER_PREFIX, TOMBSTONE_MARKER_PREFIX

_LEGACY_RECORD_MARKER = "<!-- agentrecord-record -->"
_LEGACY_ESCAPED = "<!-- agentrecord-record-text -->"

_ENTRY_MARKER = re.compile(
    rf"^{re.escape(ENTRY_MARKER_PREFIX)}([^>]+) -->",
    re.MULTILINE,
)
_TOMBSTONE_MARKER = re.compile(
    rf"^{re.escape(TOMBSTONE_MARKER_PREFIX)}([^>]+) -->",
    re.MULTILINE,
)
_RECORD = re.compile(
    r"^\*\*(\d{2}:\d{2})(?: ([^\n]*?))?:\*\*\s?(.*?)"
    r"(?=^\*\*\d{2}:\d{2}(?: [^\n]*?)?:\*\*|\Z)",
    re.MULTILINE | re.DOTALL,
)


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
    markers = [(m.start(), m.group(1)) for m in _ENTRY_MARKER.finditer(content)]
    tombstones = [m.group(1) for m in _TOMBSTONE_MARKER.finditer(content)]
    records = list(_RECORD.finditer(content))

    entries = []
    marker_index = 0
    for index, match in enumerate(records, 1):
        entry_id = None
        if marker_index < len(markers) and markers[marker_index][0] < match.start():
            entry_id = markers[marker_index][1]
            marker_index += 1
        if entry_id is None:
            entry_id = legacy_entry_id(date, index)
        tag = (match.group(2) or "").strip()
        text = match.group(3).strip()
        text = re.sub(
            rf"^{re.escape(_LEGACY_ESCAPED)}\s*$",
            _LEGACY_RECORD_MARKER,
            text,
            flags=re.MULTILINE,
        )
        entries.append(
            {
                "entry_id": entry_id,
                "date": date,
                "ts": _ts_from(date, match.group(1)),
                "tag": tag,
                "speaker": "quoted_ai" if "[AI回复]" in tag else "user",
                "text": text,
            }
        )
    return {"entries": entries, "tombstones": tombstones}