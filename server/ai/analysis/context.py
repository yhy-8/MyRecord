"""报告输入准备：按天分块的原始记录流 + 行号标注，以及周期报告路径。

原始日记是唯一事实源。周报/月报只取各自时间范围内的**完整原始记录流**（`Records/YYYY-MM-DD.md`），
每条记录标注其在所属日期文件中的实际 1-based 行号；中控据此按天分组生成 `[YYYYMMDD]` 块、块内每行
`行号: 内容`，作为唯一事实源交给 Report Agent（引用来源 `R-YYYYMMDD-行号` 与该行号一致）。
本模块不提供任何派生引用（不读近期总结、不同期周报、引用历史记录）。
"""

import datetime
import re
from pathlib import Path

from .. import journal, settings
from ...hub import render as _format  # 日记格式单一来源（标记/记录正则/总结区域）


def _log_without_summary(content: str) -> str:
    """把 `<summary>` 区域替换为固定占位（仅供每日总结提示用；报告不依赖总结）。"""
    return re.sub(
        r"<summary>.*?</summary>",
        "<summary>（已省略）</summary>",
        content,
        count=1,
        flags=re.DOTALL,
    )


def _date_span(start: datetime.date, end: datetime.date) -> list[str]:
    dates = []
    current = start
    while current <= end:
        dates.append(current.strftime("%Y-%m-%d"))
        current += datetime.timedelta(days=1)
    return dates


def _existing_logs(start: datetime.date, end: datetime.date) -> list[tuple[str, str]]:
    """返回时间范围内完整原始日记文件（原始内容，不剥离总结区域）。"""
    logs = []
    for date in _date_span(start, end):
        path = settings.DIARY_DIR / f"{date}.md"
        if path.exists():
            logs.append((date, path.read_text(encoding="utf-8")))
    return logs


def _period_span(kind: str, anchor: datetime.date) -> tuple[datetime.date, datetime.date] | None:
    """返回 `anchor` 所在自然周 / 自然月的 [start, end]。"""
    if kind == "weekly":
        start = anchor - datetime.timedelta(days=anchor.weekday())
        return start, start + datetime.timedelta(days=6)
    if kind == "monthly":
        start = anchor.replace(day=1)
        next_month = (start.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        return start, next_month - datetime.timedelta(days=1)
    return None


def _analysis_report_path(
    kind: str, start: datetime.date, end: datetime.date
) -> Path:
    if kind == "weekly":
        return (
            settings.ANALYSIS_DIR
            / "Weekly"
            / f"{start:%Y-%m-%d}_to_{end:%Y-%m-%d}.md"
        )
    return settings.ANALYSIS_DIR / "Monthly" / f"{start:%Y-%m}.md"


_RECORD_PATTERN = _format.RECORD_PATTERN  # 单一来源：见 server/hub/render.py
_MARKED_RECORD_PATTERN = re.compile(
    rf"^{re.escape(journal.RECORD_MARKER)}\s*\n"
    r"\*\*(\d{2}:\d{2})(?: ([^\n]*?))?:\*\*\s?(.*?)"
    rf"(?=^{re.escape(journal.RECORD_MARKER)}\s*\n"
    r"(?=\*\*\d{2}:\d{2}(?: [^\n]*?)?:\*\*)|\Z)",
    re.MULTILINE | re.DOTALL,
)
_VALID_RECORD_MARKER_PATTERN = re.compile(
    rf"^{re.escape(journal.RECORD_MARKER)}\s*\n"
    r"(?=\*\*\d{2}:\d{2}(?: [^\n]*?)?:\*\*)",
    re.MULTILINE,
)


def _period_records(logs: list[tuple[str, str]]) -> list[dict]:
    """解析时间范围内的完整原始记录流。

    每条记录自带：date / time / tag / speaker / text，以及 **line**（该记录在所属日期文件中的
    1-based 行号，定位到其可视头部行 `**HH:MM:**`）和稳定 ID `source_id=R-YYYYMMDD-行号`。
    """
    records = []
    for date, content in logs:
        first_marker = _VALID_RECORD_MARKER_PATTERN.search(content)
        if first_marker is None:
            matches = list(_RECORD_PATTERN.finditer(content))
        else:
            marker_index = first_marker.start()
            matches = [
                *_RECORD_PATTERN.finditer(content[:marker_index]),
                *_MARKED_RECORD_PATTERN.finditer(content[marker_index:]),
            ]
        for match in matches:
            tag = (match.group(2) or "").strip()
            speaker = "quoted_ai" if "[AI回复]" in tag else "user"
            text = match.group(3).strip()
            text = re.sub(
                rf"^{re.escape(journal.ESCAPED_RECORD_MARKER)}\s*$",
                journal.RECORD_MARKER,
                text,
                flags=re.MULTILINE,
            )
            line = content[: match.start()].count("\n") + 1
            records.append(
                {
                    "source_id": f"R-{date.replace('-', '')}-{line}",
                    "date": date,
                    "time": match.group(1),
                    "line": line,
                    "tag": tag,
                    "speaker": speaker,
                    "text": text,
                }
            )
    return records