"""客户端本地日记：本地渲染 + 按天写入与对账。

每天一个 Records/YYYY-MM-DD.md 容器。每条记录前带一个隐藏的
`<!-- myrecord-id:<id> -->` 标记，删除位置写 tombstone 占位（不含正文）。
`<summary>` 区域由服务端独占写。本地写入永不因同步失败回滚；对账（apply_delta）
只做补齐与 tombstone 移除，不把已删条目推回。

日记格式（标记常量、渲染函数、<summary> 区域）由客户端本地的 render 模块维护，
与 server/hub/render.py 独立、互不引用（客户端与服务端严格分离、各自独立部署）。
"""

import datetime
import logging
import re
from pathlib import Path

from . import config, render
from .atomic_write import atomic_write
from .file_lock import file_lock


logger = logging.getLogger(__name__)


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _valid_iso_date(value: object) -> bool:
    """判断一个值是否为合法的 YYYY-MM-DD 日期。

    date 会拼进文件名（<date>.md），含 ../ 等字符的值会让写入逃逸出 Records 目录，
    因此同步写入前只接受规范日期。
    """
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        return False
    try:
        datetime.date.fromisoformat(value)
        return True
    except ValueError:
        return False

# 日记格式：客户端本地 render.py
ENTRY_MARKER_PREFIX = render.ENTRY_MARKER_PREFIX
DEVICE_MARKER_PREFIX = render.DEVICE_MARKER_PREFIX
TOMBSTONE_MARKER_PREFIX = render.TOMBSTONE_MARKER_PREFIX
entry_block = render.entry_block
tombstone_block = render.tombstone_block
day_header = render.day_header


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
            if not _valid_iso_date(date):
                # date 用于拼文件名；非法日期（如含 ../）会让写入逃逸出 Records，防御性跳过。
                logger.warning("entry_date_invalid skips date=%r", date)
                continue
            ensure_day_file(date)
            path = day_path(date)
            content = path.read_text(encoding="utf-8")
            existing = day_entry_ids(content)
            missing = [e for e in day_entries if e["entry_id"] not in existing]
            if missing:
                # 每条块后跟一个空行，与 append_record 的逐块 + "\n" 格式一致，
                # 避免对账/扇出补写时多条记录连在一起、失去换行。
                body = "".join(entry_block(e) + "\n" for e in missing)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(body)
        for tombstone in tombstones:
            _apply_tombstone(tombstone["entry_id"], tombstone.get("date", ""))


_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)


def _existing_summary(path: Path) -> str:
    """读取目标文件里已有的 `<summary>` 正文，供重建时保留（与服务端行为一致）。"""
    if not path.exists():
        return ""
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    match = _SUMMARY_RE.search(content)
    return match.group(1).strip() if match else ""


def rebuild_records(entries: list[dict], tombstones: list[dict]) -> None:
    """从权威全量数据重建本地记录文件：按时间排序、墓碑插回原位置，并保留 summary。

    用于完整对账（pull?version=0）：把本地镜像重建为与服务端渲染一致的时间有序结构，
    修复多端离线写入导致的本地乱序（这类乱序通常来自离线批量按推送顺序而非时间顺序入库）。
    局部增量（apply_delta）仍只追加/原位替换，不整页重排，避免每次扇出都重写整个文件。
    """
    by_date: dict[str, list[dict]] = {}
    for entry in entries:
        by_date.setdefault(entry["date"], []).append(entry)
    tombs_by_date: dict[str, list[dict]] = {}
    for tombstone in tombstones:
        tombs_by_date.setdefault(tombstone.get("date", ""), []).append(tombstone)
    with file_lock(records_dir() / ".journal.lock"):
        for date in sorted(set(by_date) | set(tombs_by_date)):
            if not _valid_iso_date(date):
                # date 用于拼文件名；非法日期（如含 ../）会让写入逃逸出 Records，防御性跳过。
                logger.warning("entry_date_invalid skips date=%r", date)
                continue
            ensure_day_file(date)
            path = day_path(date)
            text = render.render_day_file(
                date,
                by_date.get(date, []),
                tombs_by_date.get(date, []),
                summary=_existing_summary(path),
            )
            atomic_write(path, text)


def _apply_tombstone(entry_id: str, date: str = "") -> None:
    prefix = re.escape(ENTRY_MARKER_PREFIX)
    dev_prefix = re.escape(DEVICE_MARKER_PREFIX)
    pattern = re.compile(
        rf"^{prefix}{re.escape(entry_id)} -->\n"
        rf"(?:{dev_prefix}[^\n]*\n)?"  # 可选：记录自带的设备名标记行
        r"[^\n]*\n",
        re.MULTILINE,
    )
    # ① 本地若已有该条：替换为 tombstone 占位符（防复活，保留原位）。
    for path in list(records_dir().glob("*.md")):
        content = path.read_text(encoding="utf-8")
        match = pattern.search(content)
        if not match:
            continue
        rebuilt = (
            content[: match.start()]
            + tombstone_block(entry_id)
            + content[match.end():]
        )
        atomic_write(path, rebuilt)
        return
    # ② 本地从未有过该条：也把占位符补写到对应日文件，保证删除历史完整同步。
    if not date or not _valid_iso_date(date):
        return
    ensure_day_file(date)
    target = day_path(date)
    content = target.read_text(encoding="utf-8")
    marker = re.escape(f"{TOMBSTONE_MARKER_PREFIX}{entry_id} -->")
    if re.search(rf"^{marker}\s*$", content, re.MULTILINE):
        return  # 已存在占位符，幂等
    with target.open("a", encoding="utf-8") as handle:
        handle.write(tombstone_block(entry_id) + "\n")
