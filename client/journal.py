"""客户端本地日记：本地渲染 + 按天写入与对账。

每天一个 Records/YYYY-MM-DD.md 容器。每条记录前带一个隐藏的
`<!-- myrecord-time:<毫秒时间戳> -->` 标记，删除位置写 tombstone 占位（不含正文）。
`<summary>` 区域由服务端独占写。本地写入永不因同步失败回滚；对账（apply_delta）
补齐缺失条目、按 tombstone 移除已删条目（同 id 内容不同以云端块替换本地块），不把已删条目推回。

日记格式（标记常量、渲染函数、<summary> 区域）由客户端本地的 render 模块维护，
与 server/hub/render.py 独立、互不引用（客户端与服务端严格分离、各自独立部署）。
"""

import datetime
import logging
import re

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


def trash_path():
    """单文件垃圾桶 ``Records/trash.md``（全部已删正文，按时间戳排序，永不清理）。"""
    return records_dir() / "trash.md"


def ensure_day_file(date: str) -> None:
    path = day_path(date)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(day_header(date), encoding="utf-8")


def append_record(entry: dict) -> None:
    """把一条新记录本地写入当天文件（原子追加，永不回滚）。**只写“今天”（UTC+8）**。"""
    date = entry["date"]
    if date != today_utc8():
        # 过期/异常：客户端从不写历史日（只读）；直接丢弃，不落盘。
        logger.warning(
            "append_record_expired skips date=%s entry_id=%s",
            date,
            entry.get("entry_id"),
        )
        return
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


def today_utc8() -> str:
    """当前 UTC+8 自然日 YYYY-MM-DD（固定时区，非配置项）。"""
    return datetime.datetime.now(tz=_UTC8).date().isoformat()


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

    作用范围**只限“今天”**（UTC+8）：`date != 今天` 的条目/墓碑直接忽略——历史日以
    整文件覆盖（云端权威），不在此增量合并/重排。

    增量对账把受影响日期**按 (ts, entry_id) 合并重排**，而不是简单追加到文件末尾：
    多端离线记录、服务端按推送顺序入库时，条目到达顺序可能与时间序不一致，
    只追加会让本地镜像乱序。这里以块为单位合并重排，既保留既有块原文与
    `<summary>`，又始终得到时间有序的镜像。

    **同 id 以云端为准**：同一 entry_id 云端返回的块与本地块内容不同时，用云端块替换
    本地块（避免云端与本地长期不一致）；内容相同则保持不动。
    """
    entry_by_date: dict[str, list[dict]] = {}
    for entry in entries:
        entry_by_date.setdefault(entry["date"], []).append(entry)
    tomb_by_date: dict[str, list[dict]] = {}
    for tombstone in tombstones:
        tomb_by_date.setdefault(tombstone.get("date", ""), []).append(tombstone)

    today = today_utc8()
    with file_lock(records_dir() / ".journal.lock"):
        for date in sorted(set(entry_by_date) | set(tomb_by_date)):
            if date != today:
                # 历史日（date < 今天）以整文件覆盖，不增量合并；date > 今天 属异常，同样跳过。
                continue
            if not _valid_iso_date(date):
                # date 用于拼文件名；非法日期（如含 ../）会让写入逃逸出 Records，防御性跳过。
                logger.warning("entry_date_invalid skips date=%r", date)
                continue
            ensure_day_file(date)
            path = day_path(date)
            content = path.read_text(encoding="utf-8")
            existing_tombs = day_tombstone_ids(content)
            header, blocks = _split_day_blocks(content, date)

            # 合并条目（云端为准）：缺失则补齐，同 id 内容不同则用云端块替换。
            for entry in entry_by_date.get(date, []):
                entry_id = entry["entry_id"]
                if entry_id in existing_tombs:
                    continue  # 防御：同 id 已是本地墓碑时不被条目复活
                server_block = entry_block(entry) + "\n"
                local_blocks = [
                    b
                    for b in blocks
                    if b[1] == entry_id and b[2].lstrip().startswith(ENTRY_MARKER_PREFIX)
                ]
                if local_blocks and all(b[2] == server_block for b in local_blocks):
                    continue
                blocks = [b for b in blocks if b[1] != entry_id]
                blocks.append((int(entry.get("ts", 0)), entry_id, server_block))
            # 墓碑：把对应条目块替换/补写为占位符，按原条目时间插回原位置；
            # 被移除的正文块同步并入本地垃圾桶（收到别端的删除也不丢正文）。
            removed_trash = []
            for tombstone in tomb_by_date.get(date, []):
                entry_id = tombstone["entry_id"]
                if entry_id in existing_tombs:
                    continue  # 已存在占位符，幂等
                removed_trash.extend(
                    b[2]
                    for b in blocks
                    if b[1] == entry_id and b[2].lstrip().startswith(ENTRY_MARKER_PREFIX)
                )
                sort_ts = int(tombstone.get("entry_ts", tombstone.get("ts", 0)))
                blocks = [b for b in blocks if b[1] != entry_id]
                blocks.append((sort_ts, entry_id, tombstone_block(entry_id) + "\n"))

            blocks.sort(key=lambda item: (item[0], item[1]))
            rebuilt = header + "".join(b[2] for b in blocks)
            if rebuilt != content:
                atomic_write(path, rebuilt)
            _append_trash_blocks(removed_trash)


# ---------- 本地解析 / 离线删除 / 单文件垃圾桶 ----------

# 条目块：`<!-- myrecord-time:id -->` + 可选设备标记 + `**HH:MM [dev]:** 正文`。
# 正文严格单行（render.entry_block 已保证），故用 MULTILINE + `$` 即可定位到行尾。
_ENTRY_BLOCK_RE = re.compile(
    r"^<!-- myrecord-time:(?P<id>[^>]+) -->\n"
    r"(?:<!-- myrecord-device:(?P<devmark>[^>]+) -->\n)?"
    r"\*\*(?P<hhmm>\d{2}:\d{2})(?: (?P<label>[^\n]*?))?:\*\* ?(?P<text>.*)$",
    re.MULTILINE,
)


def _parse_entry_block(block: str, date: str) -> dict | None:
    """把一条条目块解析回 entry dict；非条目块（墓碑/畸形）返回 None。

    id 即毫秒时间戳（数字）时 ts = int(id)；导入的裸记录 id 非数字，则从块内
    ``**HH:MM...:**`` 按 UTC+8 还原 ts（复用 _block_sort_ts 的还原逻辑）。
    """
    match = _ENTRY_BLOCK_RE.match(block.lstrip("\n"))
    if not match:
        return None
    entry_id = match.group("id")
    label = (match.group("label") or "").strip()
    device_id = match.group("devmark") or ""
    tag = ""
    if label.startswith("[") and label.endswith("]"):
        device_id = device_id or label[1:-1]
    elif label:
        tag = label
    ts = int(entry_id) if entry_id.isdigit() else _block_sort_ts(entry_id, date, block)
    return {
        "entry_id": entry_id,
        "device_id": device_id,
        "date": date,
        "ts": ts,
        "tag": tag,
        "text": match.group("text").strip(),
    }


def day_entries(date: str) -> list[dict]:
    """解析某天文件里当前有效的条目（不含墓碑），按文件出现顺序返回。"""
    path = day_path(date)
    if not path.exists():
        return []
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    _, blocks = _split_day_blocks(content, date)
    result = []
    for _ts, _entry_id, block_text in blocks:
        parsed = _parse_entry_block(block_text, date)
        if parsed is not None:
            result.append(parsed)
    return result


def latest_local_entry(date: str) -> dict | None:
    """返回本地某天 ``(ts, entry_id)`` 最大的一条有效条目（供离线删除本地最新一条）。"""
    entries = day_entries(date)
    if not entries:
        return None
    return max(entries, key=lambda e: (e["ts"], e["entry_id"]))


def _trash_sort_key(entry_id: str):
    # 垃圾桶块按原条目时间排序；正常 id 即毫秒时间戳，裸记录 id 退化为按 id 排。
    return (int(entry_id) if entry_id.isdigit() else 0, entry_id)


def _append_trash_blocks(blocks: list[str]) -> None:
    """把若干条目块并入单文件垃圾桶 ``Records/trash.md``（按时间戳排序、按 id 去重）。"""
    cleaned = []
    for block in blocks:
        match = re.match(r"\s*<!-- myrecord-time:([^>]+) -->", block)
        if not match:
            continue
        cleaned.append((_trash_sort_key(match.group(1)), block.strip("\n")))
    if not cleaned:
        return
    path = trash_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if path.exists():
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            content = ""
        matches = list(_BLOCK_START_RE.finditer(content))
        for i, match in enumerate(matches):
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
            block_text = content[start:end].strip("\n")
            if not block_text.startswith(ENTRY_MARKER_PREFIX):
                continue  # 垃圾桶里不应有墓碑，防御性跳过
            existing.append((_trash_sort_key(match.group(2)), block_text))
    ids = {key[1] for key, _ in existing}
    for key, block_text in cleaned:
        if key[1] in ids:
            continue
        existing.append((key, block_text))
        ids.add(key[1])
    existing.sort(key=lambda item: item[0])
    atomic_write(path, "".join(block + "\n" for _, block in existing))


def delete_record(entry: dict) -> bool:
    """离线删除本地某条：移除条目块、原位写墓碑、正文并入本地垃圾桶；返回是否真删除。

    仅允许删“今天”。删除后墓碑/正文由上层入 outbox 并异步推送，服务端与其它端按
    entry_id 幂等合并——因此离线删除完全可行，不依赖“服务端认定的最新一条”。
    """
    date = entry.get("date", "")
    if date != today_utc8():
        return False
    entry_id = entry["entry_id"]
    with file_lock(records_dir() / ".journal.lock"):
        path = day_path(date)
        if not path.exists():
            return False
        content = path.read_text(encoding="utf-8")
        if entry_id not in day_entry_ids(content):
            return False
        header, blocks = _split_day_blocks(content, date)
        removed = [
            block_text
            for _ts, block_id, block_text in blocks
            if block_id == entry_id and block_text.lstrip().startswith(ENTRY_MARKER_PREFIX)
        ]
        blocks = [item for item in blocks if item[1] != entry_id]
        if entry_id not in day_tombstone_ids(content):
            blocks.append((int(entry.get("ts", 0)), entry_id, tombstone_block(entry_id) + "\n"))
        blocks.sort(key=lambda item: (item[0], item[1]))
        atomic_write(path, header + "".join(item[2] for item in blocks))
        _append_trash_blocks(removed)
    return True
