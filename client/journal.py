"""客户端本地日记：本地渲染 + 按天写入与对账。

每天一个 Records/YYYY-MM-DD.md 容器。每条记录前带一个隐藏的
`<!-- myrecord-time:<毫秒时间戳> -->` 标记，删除位置写 tombstone 占位（不含正文）。
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


def day_tombstone_ids(content: str) -> set[str]:
    return set(
        re.findall(
            r"^" + re.escape(TOMBSTONE_MARKER_PREFIX) + r"([^>]+) -->",
            content,
            re.MULTILINE,
        )
    )


# 块起始：条目标记或墓碑标记行（id 紧跟其内；生产环境 id 即毫秒时间戳）。
_BLOCK_START_RE = re.compile(
    r"^(<!-- (?:myrecord-time:|myrecord-tombstone-time:)([^>]+) -->)",
    re.MULTILINE,
)


# 展示/分组统一时区：epoch 是无时区的绝对时间，记录时间统一按 UTC+8 展示。
_UTC8 = datetime.timezone(datetime.timedelta(hours=8))


def _block_sort_ts(entry_id: str, date: str, block_text: str) -> int:
    """块的时间排序键：生产环境 id 即毫秒时间戳（entry_id == str(ts)）。

    非数字 id（导入的裸记录 bare-...，ts 由显示 HH:MM 推导、分钟对齐会重复）则从
    块内 `**HH:MM:` 显示时间按 UTC+8 还原 ts，使增量重排与服务端 (derived_ts, entry_id)
    排序一致——裸记录之间 ts 常重复，entry_id 才是真正的次序键；无法还原时防御性取 0。
    """
    if entry_id.isdigit():
        return int(entry_id)
    match = re.search(r"\*\*(\d{2}:\d{2})", block_text)
    if match and _valid_iso_date(date):
        hour, minute = (int(part) for part in match.group(1).split(":"))
        year, month, day = (int(part) for part in date.split("-"))
        dt = datetime.datetime(year, month, day, hour, minute, tzinfo=_UTC8)
        return int(dt.timestamp() * 1000)
    return 0


def _split_day_blocks(content: str, date: str) -> tuple[str, list[tuple[int, str, str]]]:
    """把日记文件拆成头部文本 + 有序块列表。

    原始记录流由条目块 / 墓碑块组成，每块以 `myrecord-time:` 或
    `myrecord-tombstone-time:` 标记行起始。头部（文件头、`<summary>`、
    `## 原始记录流` 分隔等）完整保留；块的**原文**（含设备行等）也原样保留，
    只是按 (时间戳, entry_id) 重新排序，保证镜像始终时间有序。
    返回 (头部, [(sort_ts, entry_id, 块原文)])。
    """
    matches = list(_BLOCK_START_RE.finditer(content))
    if not matches:
        return content, []
    header = content[: matches[0].start()]
    blocks = []
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        block_text = content[start:end]
        entry_id = match.group(2)
        blocks.append((_block_sort_ts(entry_id, date, block_text), entry_id, block_text))
    return header, blocks


def apply_delta(entries: list[dict], tombstones: list[dict]) -> None:
    """补齐缺失条目并按 tombstone 移除本地已删条目，保持当天文件时间有序。

    增量对账把受影响日期**按 (ts, entry_id) 合并重排**，而不是简单追加到文件末尾：
    多端离线记录、服务端按推送顺序（v）入库时，条目到达顺序可能与时间序不一致，
    只追加会让本地镜像乱序（与全量重建不一致）。这里以块为单位合并重排，
    既保留既有块原文与 `<summary>`，又始终得到时间有序的镜像。
    """
    entry_by_date: dict[str, list[dict]] = {}
    for entry in entries:
        entry_by_date.setdefault(entry["date"], []).append(entry)
    tomb_by_date: dict[str, list[dict]] = {}
    for tombstone in tombstones:
        tomb_by_date.setdefault(tombstone.get("date", ""), []).append(tombstone)

    with file_lock(records_dir() / ".journal.lock"):
        for date in sorted(set(entry_by_date) | set(tomb_by_date)):
            if not _valid_iso_date(date):
                # date 用于拼文件名；非法日期（如含 ../）会让写入逃逸出 Records，防御性跳过。
                logger.warning("entry_date_invalid skips date=%r", date)
                continue
            ensure_day_file(date)
            path = day_path(date)
            content = path.read_text(encoding="utf-8")
            existing_entries = day_entry_ids(content)
            existing_tombs = day_tombstone_ids(content)
            header, blocks = _split_day_blocks(content, date)

            # 补齐缺失条目（按时间排序并入，而非追加到末尾）
            for entry in entry_by_date.get(date, []):
                if entry["entry_id"] in existing_entries:
                    continue
                blocks.append(
                    (int(entry.get("ts", 0)), entry["entry_id"], entry_block(entry) + "\n")
                )
            # 墓碑：把对应条目块替换/补写为占位符，按原条目时间插回原位置
            for tombstone in tomb_by_date.get(date, []):
                entry_id = tombstone["entry_id"]
                if entry_id in existing_tombs:
                    continue  # 已存在占位符，幂等
                sort_ts = int(tombstone.get("entry_ts", tombstone.get("ts", 0)))
                blocks = [b for b in blocks if b[1] != entry_id]
                blocks.append((sort_ts, entry_id, tombstone_block(entry_id) + "\n"))

            blocks.sort(key=lambda item: (item[0], item[1]))
            rebuilt = header + "".join(b[2] for b in blocks)
            if rebuilt != content:
                atomic_write(path, rebuilt)


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
    使多端离线写入时也按时间而非推送顺序入库。
    局部增量（apply_delta）同样按受影响日期合并重排（见 apply_delta），保证扇出后
    镜像仍时间有序；与全量重建共用同一套 (ts, entry_id) 排序约定。
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



