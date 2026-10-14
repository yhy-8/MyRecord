"""服务端权威存储：**「今天」的 ``Records/<今天>.md`` 文件即唯一事实源**。

- 「今天」的条目/墓碑以 ``Records/<今天>.md`` 整文件为准。服务端不另存一份权威
  副本：所有写操作（新增 / 删除）都在跨进程 ``.journal.lock`` + 内存锁下
  「读文件 → 按 entry_id 合并 → 原子写回」，因此进程重启、``state.json`` 丢失都
  不会丢文件里的内容。
- ``Records/trash.md``：单文件垃圾桶（全部已删正文），同样以文件为准。
- ``state.json``：**只保留鉴权设备令牌**，可随时删除；日记内容全部可由 ``Records/``
  重建（鉴权除外）。
- 变更令牌：``sha256(今天文件内容)``；只用于长轮询唤醒，不筛选数据（pull 始终返回
  今天全量，客户端按 entry_id 幂等合并）。
- 同一 entry_id 已存在（协议只有新增/删除，正常只可能是重试或毫秒碰撞）时，**以
  文件现有内容为准**：来的那条视为已处理（客户端可清 outbox），不替换、不复活，
  并记一条 ``entry_id_conflict`` 日志。客户端对账时以云端块替换本地块，使两端收敛。
"""

import contextlib
import datetime
import hashlib
import json
import logging
import re
import threading
from pathlib import Path

from .atomic_write import atomic_write

from . import auth, render


logger = logging.getLogger(__name__)


# 展示/分组与“今天”统一时区：epoch 是无时区的绝对时间，固定按 UTC+8 换算（非配置项）。
_UTC8 = datetime.timezone(datetime.timedelta(hours=8))


def today_utc8() -> str:
    """当前 UTC+8 自然日 YYYY-MM-DD（固定时区，非配置项）。"""
    return datetime.datetime.now(tz=_UTC8).date().isoformat()


def _as_int(value: object, default: int = 0) -> int:
    """把值安全转为 int；遇到 None / 非数值（畸形客户端数据）时用 default，避免 500。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalized_date(value: object, ts: int) -> str:
    """把 entry 的 date 规范为合法的 YYYY-MM-DD；非法时回退到由 ts 推导。

    date 会拼进文件名（<date>.md），禁止任何含路径分隔符/.. 的非日期值。
    """
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    return derive_date(ts)


def derive_date(ts: int) -> str:
    """由毫秒时间戳推导日期（UTC+8），仅作为缺少 date 字段时的兜底。"""
    if ts <= 0:
        return today_utc8()
    return (
        datetime.datetime.fromtimestamp(ts / 1000.0, tz=_UTC8).date().isoformat()
    )


class Store:
    def __init__(
        self,
        state_path: Path,
        records_dir: Path | None = None,
    ):
        self.path = Path(state_path)
        # 「今天」文件即权威：必须有一个 Records 目录承载它。未显式传入时默认
        # state.json 同级的 Records/，避免出现“无落盘目录”这种无处存内容的状态。
        self.records_dir = (
            Path(records_dir) if records_dir is not None else self.path.parent / "Records"
        )
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        # state.json 只保留鉴权；条目/墓碑/垃圾桶都是今天文件的解析缓存（可重建）。
        self.data = self._load()
        self._refresh_cache()

    # ---------- 加载与保存（只持久化鉴权） ----------

    def _load(self) -> dict:
        if not self.path.exists():
            default = {"devices": {}}
            self._write(default)
            return default
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 读取失败只影响鉴权；日记内容在 Records/，不因此丢失。记警告便于观测。
            logger.warning("state.json 读取失败，已回退到空状态: %s", self.path)
            value = {}
        if not isinstance(value, dict):
            value = {}
        devices = value.get("devices")
        return {"devices": devices if isinstance(devices, dict) else {}}

    def _write(self, data: dict) -> None:
        atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=2))

    def _save(self) -> None:
        self._write({"devices": self.data["devices"]})

    # ---------- 今天文件读写 ----------

    def _today_path(self) -> Path:
        return self.records_dir / f"{today_utc8()}.md"

    def _read_today(self) -> str:
        try:
            return self._today_path().read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return ""

    @contextlib.contextmanager
    def _journal_locked(self):
        """跨进程互斥（与 AI 写 <summary>、客户端本地写共用同一把锁约定）。"""
        from ..ai.file_lock import FileLock

        self.records_dir.mkdir(parents=True, exist_ok=True)
        lock = FileLock.acquire(self.records_dir / ".journal.lock", blocking=True)
        try:
            yield
        finally:
            if lock is not None:
                lock.release()

    def _read_blocks(self, today: str) -> tuple[str, list[tuple[int, str, str]]]:
        content = self._read_today()
        if content.strip():
            return render.split_day_blocks(content, today)
        return render.day_header(today), []

    @staticmethod
    def _write_blocks(path: Path, header: str, blocks: list[tuple[int, str, str]]) -> None:
        ordered = sorted(blocks, key=lambda item: (item[0], item[1]))
        atomic_write(path, header + "".join(block[2] for block in ordered))

    def _refresh_cache(self) -> None:
        """从今天文件重建内存缓存（state.json 只存鉴权）。"""
        today = today_utc8()
        _header, entries, tombstones = render.parse_day(self._read_today(), today)
        with self._lock:
            self.data["entries"] = {e["entry_id"]: e for e in entries}
            self.data["tombstones"] = {t["entry_id"]: t for t in tombstones}

    def _append_trash(self, bodies: list[dict]) -> None:
        """把若干被删正文并入单文件垃圾桶 ``Records/trash.md``（按时间戳排序、按 id 去重）。"""
        path = self.records_dir / "trash.md"
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            content = ""
        _header, blocks = render.split_day_blocks(content, "")
        existing = []
        for _ts, block_id, block_text in blocks:
            if not render.is_entry_block(block_text):
                continue
            key = int(block_id) if block_id.isdigit() else 0
            existing.append((key, block_id, block_text.strip("\n")))
        ids = {block_id for _key, block_id, _text in existing}
        for body in bodies:
            entry_id = body.get("entry_id")
            if not entry_id or entry_id in ids:
                continue
            key = int(entry_id) if str(entry_id).isdigit() else 0
            existing.append((key, entry_id, render.entry_block(body).strip("\n")))
            ids.add(entry_id)
        existing.sort(key=lambda item: (item[0], item[1]))
        self.records_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(path, "".join(text + "\n" for _key, _block_id, text in existing))

    # ---------- 设备与令牌（唯一持久化内容） ----------

    def register_device(self, name: str, token: str) -> str:
        """签发链接凭证（单一凭证模型）。

        服务端只存在唯一一个 token：签发新 token 会直接覆盖并删除旧 token，
        旧凭证立即失效。返回设备标签（仅用于展示/管理）。
        """
        with self._lock:
            device_id = auth.slugify(name) or "device"
            self.data["devices"] = {
                device_id: {
                    "created_at": int(datetime.datetime.now().timestamp()),
                    "token_hash": auth.hash_token(token),
                    "active": True,
                }
            }
            self._save()
            return device_id

    def verify_device(self, device_id: str, token: str) -> bool:
        """校验链接凭证。凭证不绑定具体设备，token 匹配任一活动凭证即放行。"""
        with self._lock:
            for record in self.data["devices"].values():
                if record.get("active") is not True:
                    continue
                if auth.verify_token(token, record["token_hash"]):
                    return True
            return False

    def device_ids(self) -> list[str]:
        with self._lock:
            return sorted(
                device_id
                for device_id, record in self.data["devices"].items()
                if record.get("active") is True
            )

    def active_credential(self) -> dict | None:
        """返回当前唯一有效链接凭证的信息（device_id + created_at），无则 None。"""
        with self._lock:
            for device_id, record in self.data["devices"].items():
                if record.get("active") is True:
                    return {
                        "device_id": device_id,
                        "created_at": int(record.get("created_at") or 0),
                    }
            return None

    # ---------- 条目 ----------

    def append_entries(self, device_id: str, entries: list[dict]) -> tuple[list[str], list[str]]:
        """按 entry_id 合并多个条目（**仅今天**），返回 (accepted, rejected)。

        - accepted：实际新增、或已被权威处理（已删或已存在）的 entry_id，客户端可清 outbox。
        - rejected：``date != 今天`` 的 entry_id（过期/异常），未入库。批次内只要含过期条目
          即整体拒绝（客户端作废后单独重推有效条目）。

        已删（墓碑）或已存在（同 id）的条目都不重新入库：前者防复活，后者以文件（云端）
        现有内容为准（同 id 内容不同只可能是重试/碰撞，记录日志，服务端不替换；客户端对账时
        用云端块替换本地块）。
        """
        today = today_utc8()
        added: list[str] = []
        superseded: list[str] = []
        rejected: list[str] = []
        for entry in entries:
            entry_id = entry.get("entry_id")
            if not entry_id:
                continue
            date = _normalized_date(entry.get("date"), _as_int(entry.get("ts")))
            if date != today:
                rejected.append(entry_id)
        if rejected:
            return [], rejected

        with self._journal_locked(), self._lock:
            _header, blocks = self._read_blocks(today)
            entry_ids = {
                block_id for _ts, block_id, text in blocks if render.is_entry_block(text)
            }
            tomb_ids = {
                block_id for _ts, block_id, text in blocks if not render.is_entry_block(text)
            }
            for entry in entries:
                entry_id = entry.get("entry_id")
                if not entry_id:
                    continue
                if entry_id in tomb_ids:
                    superseded.append(entry_id)
                    continue
                if entry_id in entry_ids:
                    self._warn_if_conflict(entry_id, entry, blocks, today)
                    superseded.append(entry_id)
                    continue
                record = {
                    "entry_id": entry_id,
                    "device_id": device_id,
                    "date": today,
                    "ts": _as_int(entry.get("ts")),
                    "tag": entry.get("tag", ""),
                    "text": entry.get("text", ""),
                }
                blocks.append((record["ts"], entry_id, render.entry_block(record) + "\n"))
                entry_ids.add(entry_id)
                added.append(entry_id)
            if added:
                self._write_blocks(self._today_path(), _header, blocks)
                self._refresh_cache()
                self._changed.notify_all()
        return added + superseded, rejected

    def _warn_if_conflict(self, entry_id, entry, blocks, today) -> None:
        """同 id 已存在：若正文/标签不同，记一条冲突日志（不改文件，文件现有内容为准）。"""
        for _ts, block_id, block_text in blocks:
            if block_id != entry_id or not render.is_entry_block(block_text):
                continue
            existing = render.parse_entry_block(block_text, today)
            if existing is None:
                return
            if existing.get("text") != (entry.get("text") or "").strip() or existing.get(
                "tag"
            ) != (entry.get("tag") or "").strip():
                logger.warning(
                    "entry_id_conflict entry_id=%s action=keep_file", entry_id
                )
            return

    def tombstone(
        self, entry_id: str, deleted_by: str, entry_body: dict | None = None
    ) -> bool:
        """为某 entry_id 写 tombstone 并把正文移入垃圾桶（**仅今天可删**），返回是否新产生。

        - entry 在今天文件里：正文取自文件。
        - entry 不在（客户端离线新建、尚未推送就删除）：正文取自客户端随删除一并发来的
          ``entry_body``。
        - 两者都没有：无可删内容，返回 False。
        - 已有墓碑：幂等返回 False。
        """
        today = today_utc8()
        with self._journal_locked(), self._lock:
            _header, blocks = self._read_blocks(today)
            tomb_ids = {
                block_id for _ts, block_id, text in blocks if not render.is_entry_block(text)
            }
            if entry_id in tomb_ids:
                return False
            entry_blocks = [
                (ts, text)
                for ts, block_id, text in blocks
                if block_id == entry_id and render.is_entry_block(text)
            ]
            parsed = (
                render.parse_entry_block(entry_blocks[0][1], today)
                if entry_blocks
                else None
            )
            body = parsed or entry_body
            if body is None:
                return False
            date = (body or {}).get("date") or today
            if date != today:
                return False
            blocks = [
                item
                for item in blocks
                if not (item[1] == entry_id and render.is_entry_block(item[2]))
            ]
            sort_ts = _as_int((body or {}).get("ts")) or (
                entry_blocks[0][0] if entry_blocks else 0
            )
            blocks.append((sort_ts, entry_id, render.tombstone_block(entry_id) + "\n"))
            self._write_blocks(self._today_path(), _header, blocks)
            self._append_trash([body])
            self._refresh_cache()
            self._changed.notify_all()
        return True

    def apply_deletes(
        self, device_id: str, deletes: list[dict]
    ) -> tuple[list[str], list[str]]:
        """批量应用客户端删除（tombstone 以 entry_id 为准，**仅今天**），返回 (accepted, rejected)。

        每个 delete 形如 ``{entry_id, date, entry}``：``entry`` 是被删正文（离线新建后即删、
        云端从未有过时据此把正文放进垃圾桶）。与 append_entries 同样的批次拒绝语义。
        """
        today = today_utc8()
        accepted: list[str] = []
        rejected: list[str] = []
        for item in deletes:
            entry_id = item.get("entry_id")
            if not entry_id:
                continue
            body = item.get("entry") or {}
            date = _normalized_date(
                item.get("date") or body.get("date"), _as_int(body.get("ts"))
            )
            if date != today:
                rejected.append(entry_id)
        if rejected:
            return [], rejected

        with self._journal_locked(), self._lock:
            _header, blocks = self._read_blocks(today)
            tomb_ids = {
                block_id for _ts, block_id, text in blocks if not render.is_entry_block(text)
            }
            trash_bodies: list[dict] = []
            for item in deletes:
                entry_id = item.get("entry_id")
                if not entry_id:
                    continue
                if entry_id in tomb_ids:
                    accepted.append(entry_id)
                    continue
                entry_blocks = [
                    (ts, text)
                    for ts, block_id, text in blocks
                    if block_id == entry_id and render.is_entry_block(text)
                ]
                parsed = (
                    render.parse_entry_block(entry_blocks[0][1], today)
                    if entry_blocks
                    else None
                )
                body = parsed or item.get("entry")
                blocks = [
                    block
                    for block in blocks
                    if not (
                        block[1] == entry_id and render.is_entry_block(block[2])
                    )
                ]
                sort_ts = _as_int((body or {}).get("ts")) or (
                    entry_blocks[0][0] if entry_blocks else 0
                )
                blocks.append(
                    (sort_ts, entry_id, render.tombstone_block(entry_id) + "\n")
                )
                tomb_ids.add(entry_id)
                if body is not None:
                    trash_bodies.append(body)
                accepted.append(entry_id)
            if accepted:
                self._write_blocks(self._today_path(), _header, blocks)
                if trash_bodies:
                    self._append_trash(trash_bodies)
                self._refresh_cache()
                self._changed.notify_all()
        return accepted, rejected

    # ---------- 对账 / 拉取 ----------

    def _token_locked(self) -> str:
        return hashlib.sha256(self._read_today().encode("utf-8")).hexdigest()

    def today_state(self) -> dict:
        """当前「今天」的权威全量：全部条目 + 全部墓碑（按时间有序）+ 变更令牌。

        直接解析今天文件，状态与扇出均以文件内容为准。
        """
        today = today_utc8()
        with self._lock:
            content = self._read_today()
            _header, entries, tombstones = render.parse_day(content, today)
            token = hashlib.sha256(content.encode("utf-8")).hexdigest()
        entries.sort(key=lambda e: (_as_int(e.get("ts")), e["entry_id"]))
        tombstones.sort(key=lambda t: (_as_int(t.get("entry_ts")), t["entry_id"]))
        return {"token": token, "entries": entries, "tombstones": tombstones}

    def pull(self, after_token: str = "") -> dict:
        """返回「今天」的全量条目与墓碑（令牌不筛选数据，客户端按 entry_id 幂等合并）。"""
        return self.today_state()

    def wait_for_change(self, after_token: str, timeout: float = 25.0) -> dict:
        """长轮询：令牌与请求值不一致（有新写入，或客户端令牌超前）即返回今天全量；
        完全一致则挂起至超时并返回空增量（仅带令牌）。

        用 ``!=`` 而非比较大小：客户端令牌超前（如今天文件被替换/重置）时也立即返回
        全量让它重新对齐，而不是白等满一个超时。
        """
        with self._changed:
            if self._token_locked() != after_token:
                return self.today_state()
            self._changed.wait(timeout)
            token = self._token_locked()
            if token != after_token:
                return self.today_state()
            return {"token": token, "entries": [], "tombstones": []}

    def today_file_counts(self) -> tuple[int, int]:
        """从「今天」的 Records 文件解析条目数与墓碑数（计数以文件为准）。"""
        content = self._read_today()
        entry_re = re.compile(
            r"^" + re.escape(render.ENTRY_MARKER_PREFIX) + r"[^>]+ -->",
            re.MULTILINE,
        )
        tomb_re = re.compile(
            r"^" + re.escape(render.TOMBSTONE_MARKER_PREFIX) + r"[^>]+ -->",
            re.MULTILINE,
        )
        return len(entry_re.findall(content)), len(tomb_re.findall(content))

    def snapshot(self) -> dict:
        state = self.today_state()
        with self._lock:
            return {
                "token": state["token"],
                "entries": {e["entry_id"]: e for e in state["entries"]},
                "tombstones": {t["entry_id"]: t for t in state["tombstones"]},
                "devices": {
                    device_id: {
                        "active": record.get("active"),
                        "created_at": record.get("created_at"),
                    }
                    for device_id, record in self.data["devices"].items()
                },
            }
