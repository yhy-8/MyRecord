"""服务端权威条目存储：append-only 条目、tombstone、垃圾桶与设备令牌。

单一 JSON 状态文件，原子替换。条目按 entry_id 去重合并；tombstone 只删引用、
正文移入垃圾桶（可恢复）。version 作为全局同步游标，供拉取/对账。
"""

import datetime
import json
import logging
import os
import re
import threading
import uuid
from pathlib import Path

from . import auth


logger = logging.getLogger(__name__)


class Store:
    def __init__(self, state_path: Path):
        self.path = state_path
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self.data = self._load()

    # ---------- 加载与保存 ----------

    def _load(self) -> dict:
        if not self.path.exists():
            default = {
                "version": 0,
                "entries": {},
                "tombstones": {},
                "trash": {},
                "devices": {},
            }
            self._write(default)
            return default
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # state.json 是唯一事实源：损坏时不能静默当成“空库”，否则会看似全部丢失。
            # 记录告警（仍按原逻辑回退到空状态，写入是原子替换，正常不触发，但需可观测）。
            logger.warning("state.json 读取失败，已回退到空状态: %s", self.path)
            value = {}
        for key in ("version", "entries", "tombstones", "trash", "devices"):
            if key not in value:
                value[key] = 0 if key == "version" else {}
        return value

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(
            self.path.name + f".{uuid.uuid4().hex}.tmp"
        )
        try:
            tmp_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp_path, self.path)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _save(self) -> None:
        self._write(self.data)

    # ---------- 设备与令牌 ----------

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
        """校验链接凭证。

        凭证不绑定具体设备：只要 token 匹配任一活动凭证即放行。device_id 是
        客户端自报的本机名（用于条目归属与展示），不参与令牌匹配——符合“只签发
        一个链接凭证，各端用本机名区分”的模型。
        """
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

    def device_names(self) -> list[str]:
        """返回真实设备名集合（去重自条目/垃圾桶/删除标记），供状态展示。

        credential 标签（见 active_credential）只是连接凭证的标识，并非设备；
        设备名是各端自报的本机名（写入条目 / 删除标记），因此从它们归纳。
        """
        with self._lock:
            names = set()
            for entry in self.data["entries"].values():
                if entry.get("device_id"):
                    names.add(entry["device_id"])
            for entry in self.data["trash"].values():
                if entry.get("device_id"):
                    names.add(entry["device_id"])
            for tomb in self.data["tombstones"].values():
                if tomb.get("deleted_by"):
                    names.add(tomb["deleted_by"])
            return sorted(names)

    # ---------- 条目 ----------

    def append_entries(self, device_id: str, entries: list[dict]) -> list[str]:
        """按 entry_id 去重合并多个条目，返回实际新增的 entry_id 列表。"""
        with self._lock:
            added = []
            for entry in entries:
                entry_id = entry.get("entry_id")
                if not entry_id or entry_id in self.data["entries"]:
                    continue
                self.data["version"] += 1
                self.data["entries"][entry_id] = {
                    "entry_id": entry_id,
                    "device_id": device_id,
                    "date": str(entry.get("date") or derive_date(int(entry.get("ts", 0)))),
                    "ts": int(entry.get("ts", 0)),
                    "tag": entry.get("tag", ""),
                    "text": entry.get("text", ""),
                    "v": self.data["version"],
                }
                added.append(entry_id)
            if added:
                self._save()
                self._changed.notify_all()
            return added

    def tombstone(self, entry_id: str, deleted_by: str) -> bool:
        """把条目移入垃圾桶并写 tombstone，返回是否成功。"""
        with self._lock:
            entry = self.data["entries"].get(entry_id)
            if entry is None or entry_id in self.data["tombstones"]:
                return False
            self.data["trash"][entry_id] = entry
            del self.data["entries"][entry_id]
            self.data["version"] += 1
            self.data["tombstones"][entry_id] = {
                "entry_id": entry_id,
                "deleted_by": deleted_by,
                "date": entry["date"],
                "v": self.data["version"],
                "ts": int(datetime.datetime.now().timestamp()),
            }
            self._save()
            self._changed.notify_all()
            return True

    def latest_entry_for_date(self, ts_date: str) -> dict | None:
        """返回某日期（YYYY-MM-DD）内最新一条 entry，供 /d 删除。"""
        with self._lock:
            candidates = []
            for entry in self.data["entries"].values():
                if entry["date"] == ts_date:
                    candidates.append(entry)
            if not candidates:
                return None
            return max(candidates, key=lambda e: (e["ts"], e["entry_id"]))

    # ---------- 对账 / 拉取 ----------

    def pull(self, after_version: int) -> dict:
        """返回 version 在 (after_version, 当前] 内的条目与 tombstone。"""
        with self._lock:
            entries = [
                value
                for value in self.data["entries"].values()
                if value["v"] > after_version
            ]
            tombstones = [
                value
                for value in self.data["tombstones"].values()
                if value["v"] > after_version
            ]
            return {
                "version": self.data["version"],
                "entries": sorted(entries, key=lambda e: e["v"]),
                "tombstones": sorted(tombstones, key=lambda t: t["v"]),
            }

    def wait_for_change(self, after_version: int, timeout: float = 25.0) -> dict:
        """等待新数据到来（长轮询），超时返回当前数据。"""
        with self._changed:
            if self.data["version"] > after_version:
                return self.pull(after_version)
            self._changed.wait(timeout)
            return self.pull(after_version)

    def render_records(self, records_dir: Path, trash_dir: Path) -> None:
        """把条目与 tombstone 渲染成每天 Records 文件，并把已删正文渲染进垃圾桶。

        渲染会保留目标文件已有的 `<summary>`（由 AI 日总结写入）。渲染来源只有
        state.json，若直接重建会抹掉刚生成的总结，因此渲染前读取旧文件里
        的 summary 并在重写时带回。
        """
        from . import render as render_mod

        records_dir.mkdir(parents=True, exist_ok=True)
        trash_dir.mkdir(parents=True, exist_ok=True)

        # 在 self._lock 内对权威状态做一次快照，避免渲染线程与 HTTP push/delete 并发
        # 迭代同一 dict 而触发 “dictionary changed size during iteration”。
        with self._lock:
            entries = list(self.data["entries"].values())
            tombs = list(self.data["tombstones"].values())
            trash = list(self.data["trash"].values())

        entries_by_date: dict[str, list] = {}
        for entry in entries:
            entries_by_date.setdefault(entry["date"], []).append(entry)
        tombs_by_date: dict[str, list] = {}
        for tomb in tombs:
            tombs_by_date.setdefault(tomb["date"], []).append(tomb)
        trash_by_date: dict[str, list] = {}
        for entry in trash:
            trash_by_date.setdefault(entry["date"], []).append(entry)

        dates = sorted(set(entries_by_date) | set(tombs_by_date))
        # 与 ai/journal.update_summary_for_date 共用同一把 .journal.lock（跨进程互斥），
        # 保证“读旧总结 → 写回”期间不被并发的日总结写入覆盖（避免丢失更新的竞态）。
        from ..ai.file_lock import FileLock

        lock = FileLock.acquire(records_dir / ".journal.lock", blocking=True)
        try:
            for date in dates:
                target = records_dir / f"{date}.md"
                text = render_mod.render_day_file(
                    date,
                    entries_by_date.get(date, []),
                    tombs_by_date.get(date, []),
                    summary=_existing_summary(target),
                )
                self._atomic_write(target, text)
        finally:
            if lock is not None:
                lock.release()
        for date, trash_entries in trash_by_date.items():
            blocks = "".join(
                render_mod.entry_block(e)
                for e in sorted(
                    trash_entries, key=lambda e: (e["ts"], e["entry_id"])
                )
            )
            self._atomic_write(trash_dir / f"{date}.md", blocks)

    @staticmethod
    def _atomic_write(target: Path, text: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + f".{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, target)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": self.data["version"],
                "entries": dict(self.data["entries"]),
                "tombstones": dict(self.data["tombstones"]),
                "devices": {
                    device_id: {"active": record.get("active"), "created_at": record.get("created_at")}
                    for device_id, record in self.data["devices"].items()
                },
            }


_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)


def _existing_summary(path: Path) -> str:
    """读取目标文件里已有的 `<summary>` 正文，供渲染时保留。"""
    if not path.exists():
        return ""
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    match = _SUMMARY_RE.search(content)
    return match.group(1).strip() if match else ""


def derive_date(ts: int) -> str:
    """由秒级时间戳推导日期（UTC），仅作为缺少 date 字段时的兜底。"""
    if ts <= 0:
        return datetime.date.today().isoformat()
    return (
        datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
        .date()
        .isoformat()
    )