"""Report input preparation, source resolution, and period paths."""

import datetime
import hashlib
import json
import re
from pathlib import Path

from .. import journal, settings


def _log_without_summary(content: str) -> str:
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
    logs = []
    for date in _date_span(start, end):
        path = settings.DIARY_DIR / f"{date}.md"
        if path.exists():
            logs.append((date, _log_without_summary(path.read_text(encoding="utf-8"))))
    return logs


def _referenced_source_records(
    logs: list[tuple[str, str]], max_characters: int = 30000
) -> list[dict]:
    """Parse referenced diaries into addressable records within a bounded context."""
    reference_pattern = re.compile(
        r"^\*\*\d{2}:\d{2} \[引用\]:\*\* \[[^\]]+\]\(<([^>]+)>\)",
        re.MULTILINE,
    )
    allowed_root = settings.DIARY_DIR.resolve()
    seen_paths = set()
    seen_source_ids = set()
    records: list[dict] = []
    size = 0

    for _, content in logs:
        for relative_path in reference_pattern.findall(content):
            source_path = (settings.DIARY_DIR / relative_path).resolve()
            if source_path in seen_paths or source_path.suffix.lower() != ".md":
                continue
            if not source_path.is_relative_to(allowed_root):
                continue
            if not source_path.is_file():
                continue
            try:
                source_date = datetime.date.fromisoformat(source_path.stem).isoformat()
            except ValueError:
                continue
            try:
                source_content = source_path.read_text(encoding="utf-8")
            except OSError:
                continue
            parsed = _period_records(
                [(source_date, _log_without_summary(source_content)[:12000])]
            )
            for record in parsed:
                if record["source_id"] in seen_source_ids:
                    continue
                record_size = len(json.dumps(record, ensure_ascii=False))
                if size + record_size > max_characters:
                    return records
                records.append(record)
                seen_source_ids.add(record["source_id"])
                size += record_size
            seen_paths.add(source_path)
            if len(seen_paths) == 10:
                return records
    return records


def _referenced_records_context(records: list[dict]) -> str:
    if not records:
        return "（本周期没有可读取的显式引用来源）"
    return "\n\n".join(
        f"[{record['source_id']}] {record['date']} {record['time']}\n{record['text']}"
        for record in records
    )


def _referenced_source_context(
    logs: list[tuple[str, str]], max_characters: int = 30000
) -> str:
    """读取本周期标准引用指向的日记；拒绝日记目录以外的路径。"""
    return _referenced_records_context(
        _referenced_source_records(logs, max_characters=max_characters)
    )


def _recent_summary_context(
    before: datetime.date, days: int = 30, max_characters: int = 20000
) -> str:
    start = before - datetime.timedelta(days=days)
    sections = []
    size = 0
    dates = _date_span(start, before - datetime.timedelta(days=1))
    for date in reversed(dates):
        path = settings.DIARY_DIR / f"{date}.md"
        if not path.exists():
            continue
        summary = journal.extract_summary(path.read_text(encoding="utf-8"))
        if summary not in ("(无总结)", "暂无今日总结。"):
            section = f"### {date}\n{summary}"
            if size + len(section) > max_characters:
                break
            sections.append(section)
            size += len(section)
    sections.reverse()
    return "\n\n".join(sections) or "（没有可用的历史总结）"


def _analysis_report_path(
    kind: str, start: datetime.date, end: datetime.date, origin: str
) -> Path:
    suffix = {"manual": "manual", "auto": "auto"}[origin]
    if kind == "weekly":
        return (
            settings.ANALYSIS_DIR
            / "Weekly"
            / f"{start:%Y-%m-%d}_to_{end:%Y-%m-%d}_{suffix}.md"
        )
    return settings.ANALYSIS_DIR / "Monthly" / f"{start:%Y-%m}_{suffix}.md"


def analysis_report_path(
    kind: str, anchor: datetime.date, origin: str = "manual"
) -> Path | None:
    """返回报告确定路径，供生成前确认是否覆盖。"""
    if origin not in ("manual", "auto"):
        return None
    if kind == "weekly":
        start = anchor - datetime.timedelta(days=anchor.weekday())
        end = start + datetime.timedelta(days=6)
    elif kind == "monthly":
        start = anchor.replace(day=1)
        next_month = (start.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        end = next_month - datetime.timedelta(days=1)
    else:
        return None
    return _analysis_report_path(kind, start, end, origin)


def _monthly_supporting_reports(
    start: datetime.date, end: datetime.date, max_characters: int = 30000
) -> str:
    """Read only retrospective sections from complete in-month weekly reports."""
    candidates: dict[tuple[datetime.date, datetime.date], Path] = {}
    weekly_dir = settings.ANALYSIS_DIR / "Weekly"
    for path in sorted(weekly_dir.glob("*.md")):
        match = re.fullmatch(
            r"(\d{4}-\d{2}-\d{2})_to_(\d{4}-\d{2}-\d{2})_(?:manual|auto)",
            path.stem,
        )
        if not match:
            continue
        try:
            report_start = datetime.date.fromisoformat(match.group(1))
            report_end = datetime.date.fromisoformat(match.group(2))
        except ValueError:
            continue
        if report_start < start or report_end > end:
            continue
        period = (report_start, report_end)
        previous = candidates.get(period)
        if previous is None or path.stem.endswith("_manual"):
            candidates[period] = path

    sections = []
    size = 0
    for path in (candidates[period] for period in sorted(candidates)):
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        retrospective_match = re.search(
            r"^## 一、整理与回顾\s*\n(.*?)(?=^## 二、领域探索与研究\s*$|\Z)",
            content,
            re.MULTILINE | re.DOTALL,
        )
        if not retrospective_match:
            continue
        retrospective_text = retrospective_match.group(1).strip()
        if not retrospective_text:
            continue
        section = f"### {path.name}\n{retrospective_text[:12000]}"
        if size + len(section) > max_characters:
            break
        sections.append(section)
        size += len(section)
    return "\n\n".join(sections) or "（没有可用的同期周报）"


_RECORD_PATTERN = re.compile(
    r"^\*\*(\d{2}:\d{2})(?: ([^\n]*?))?:\*\*\s?(.*?)"
    r"(?=^\*\*\d{2}:\d{2}(?: [^\n]*?)?:\*\*|\Z)",
    re.MULTILINE | re.DOTALL,
)
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
    """Parse immutable report input into addressable journal records."""
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
        for index, match in enumerate(matches, 1):
            tag = (match.group(2) or "").strip()
            speaker = "quoted_ai" if "[AI回复]" in tag else "user"
            text = match.group(3).strip()
            text = re.sub(
                rf"^{re.escape(journal.ESCAPED_RECORD_MARKER)}\s*$",
                journal.RECORD_MARKER,
                text,
                flags=re.MULTILINE,
            )
            fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "date": date,
                        "time": match.group(1),
                        "tag": tag,
                        "speaker": speaker,
                        "text": text,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()[:12]
            records.append(
                {
                    "source_id": (
                        f"R-{date.replace('-', '')}-{index:03d}-{fingerprint}"
                    ),
                    "path": f"{date}.md",
                    "date": date,
                    "time": match.group(1),
                    "record_index": index,
                    "tag": tag,
                    "speaker": speaker,
                    "text": text,
                }
            )
    return records


def _record_chunks(records: list[dict], max_characters: int = 24000) -> list[list[dict]]:
    if max_characters < 1000:
        raise ValueError("记录分块上限不能小于 1000 字符")
    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_size = 0
    for record in records:
        serialized_size = len(json.dumps(record, ensure_ascii=False))
        parts = [record]
        if serialized_size > max_characters:
            text = str(record.get("text", ""))
            overhead = len(
                json.dumps({**record, "text": "", "text_part": "1/1"}, ensure_ascii=False)
            )
            part_size = max(1, max_characters - overhead - 32)
            text_parts = [
                text[index : index + part_size]
                for index in range(0, len(text), part_size)
            ] or [""]
            parts = [
                {
                    **record,
                    "text": text_part,
                    "text_part": f"{index}/{len(text_parts)}",
                }
                for index, text_part in enumerate(text_parts, 1)
            ]
        for part in parts:
            size = len(json.dumps(part, ensure_ascii=False))
            if current and current_size + size > max_characters:
                chunks.append(current)
                current = []
                current_size = 0
            current.append(part)
            current_size += size
    if current:
        chunks.append(current)
    return chunks
