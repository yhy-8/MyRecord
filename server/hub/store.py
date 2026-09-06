"""服务端权威条目存储：append-only 条目、tombstone、垃圾桶与设备令牌。

单一 JSON 状态文件，原子替换。条目按 entry_id 去重合并；tombstone 只删引用、
正文移入垃圾桶（可恢复）。version 作为全局同步游标，供拉取/对账。
"""

import datetime
import json
import logging
import re
import threading
from pathlib import Path

from .atomic_write import atomic_write

from . import auth


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


class Store:
    def __init__(
        self,
        state_path: Path,
        records_dir: Path | None = None,
        trash_dir: Path | None = None,
    ):
        self.path = state_path
        self.records_dir = records_dir
        self.trash_dir = trash_dir
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self.data = self._load()
        # “今天”缓存：首次访问某方法时检测到日界切换则封存（见 _maybe_seal_previous_day）。
        # 初始为 None，使进程重启落在新一天时也能正确封存历史数据。
        self._today = None
        # 最后一次渲染到的权威版本：后台用它在“写后已即时渲染”时跳过无谓的全量重渲。
        self._rendered_version = int(self.data.get("version", 0))

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
        atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=2))

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
        设备名是各端自报的本机名，随条目/删除标记一起作为标签记录，**不单独维护**。
        历史日封存后其条目态被清理，设备名也随之消失（服务端不额外记录设备清单）。
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

    def _maybe_seal_previous_day(self) -> None:
        """日界切换：进程内首次遇到新的一天时，封存今天之前的条目态。

        封存 = ① 确保历史日 Records/Trash 已落盘（无则补渲染一次）；② 从 state.json
        移除 ``date < 今天`` 的 entries/tombstones/trash；``version`` 保持单调（不递增）。
        这样 state.json 只常驻今天的数据，历史日以 ``Records/*.md`` 整文件为权威（可承载旧格式）。

        幂等：先渲染、后清理；已在今天或本进程已封存过则直接返回。
        """
        today = today_utc8()
        if today == self._today:
            return
        with self._lock:
            stale = set()
            for entry in self.data["entries"].values():
                if entry.get("date") and entry["date"] < today:
                    stale.add(entry["date"])
            for tomb in self.data["tombstones"].values():
                if tomb.get("date") and tomb["date"] < today:
                    stale.add(tomb["date"])
            for entry in self.data["trash"].values():
                if entry.get("date") and entry["date"] < today:
                    stale.add(entry["date"])
        if stale:
            # 先确保历史日文件已落盘（render 一次），再清理条目态；避免清理后文件缺失。
            self._render_dates(stale)
        with self._lock:
            entries = {
                k: v for k, v in self.data["entries"].items() if v["date"] >= today
            }
            tombstones = {
                k: v for k, v in self.data["tombstones"].items() if v.get("date", "") >= today
            }
            trash = {
                k: v for k, v in self.data["trash"].items() if v["date"] >= today
            }
            if (
                entries != self.data["entries"]
                or tombstones != self.data["tombstones"]
                or trash != self.data["trash"]
            ):
                self.data["entries"] = entries
                self.data["tombstones"] = tombstones
                self.data["trash"] = trash
                self._save()
        self._today = today

    # ---------- 条目 ----------

    def append_entries(self, device_id: str, entries: list[dict]) -> tuple[list[str], list[str]]:
        """按 entry_id 去重合并多个条目（**仅今天**），返回 (accepted, rejected)。

        - accepted：实际新增、或已被权威处理（已删或已存在）的 entry_id，客户端可据此清 outbox。
        - rejected：``date != 今天`` 的 entry_id（过期/异常），未入库、未渲染；客户端应作废。
          服务端据此拒绝批次（双保险，配合客户端过期即作废）。

        已删（tombstone）或已存在（已同步过）的条目都不能再被重复入库：entry_id 已在墓碑里时
        视为「已被权威处理（删除）」，不重新加入 entries——否则删除会被离线重推回滚；
        entry_id 已在 entries 里时视为「已被权威处理（已存在）」，亦不重复入库。
        这类条目仍上报 accepted，让客户端把它从 outbox 清掉，避免永久重试。
        """
        added = []
        superseded = []
        rejected = []
        affected = set()
        self._maybe_seal_previous_day()
        today = today_utc8()
        with self._lock:
            # 第一遍：先发现过期/异常条目。若批次含 `date != 今天` 的条目，**整体拒绝**
            # 该批次（不接受任何条目），返回 422 语义——避免“有效条目已被接受但客户端还
            # 留在 outbox 盲重试”。过期条目会被客户端作废，有效条目下次单独重推即可入库。
            for entry in entries:
                entry_id = entry.get("entry_id")
                if not entry_id:
                    continue
                date = _normalized_date(entry.get("date"), _as_int(entry.get("ts")))
                if date != today:
                    rejected.append(entry_id)
            if rejected:
                return [], rejected
            # 第二遍：全部合法（date == 今天），按 entry_id 去重合并。
            for entry in entries:
                entry_id = entry.get("entry_id")
                if not entry_id:
                    continue
                if entry_id in self.data["tombstones"]:
                    # 已删：不复活，但视为已被权威处理（客户端可清 outbox）。
                    superseded.append(entry_id)
                    continue
                if entry_id in self.data["entries"]:
                    # 已存在（已同步过）：不得重复入库，但视为已被权威处理，客户端可清 outbox。
                    superseded.append(entry_id)
                    continue
                date = _normalized_date(entry.get("date"), _as_int(entry.get("ts")))
                self.data["version"] += 1
                self.data["entries"][entry_id] = {
                    "entry_id": entry_id,
                    "device_id": device_id,
                    # date 用于拼接文件名（<date>.md），必须规范为 YYYY-MM-DD，
                    # 否则含 ../ 等字符的 date 会让渲染写出数据目录之外。
                    "date": date,
                    "ts": _as_int(entry.get("ts")),
                    "tag": entry.get("tag", ""),
                    "text": entry.get("text", ""),
                    "v": self.data["version"],
                }
                added.append(entry_id)
                affected.add(date)
            if added:
                self._save()
                self._changed.notify_all()
        if added:
            self._render_dates(affected)
        return added + superseded, rejected

    def tombstone(self, entry_id: str, deleted_by: str) -> bool:
        """把条目移入垃圾桶并写 tombstone（**仅今天可删**），返回是否成功。"""
        self._maybe_seal_previous_day()
        today = today_utc8()
        with self._lock:
            entry = self.data["entries"].get(entry_id)
            if entry is None or entry_id in self.data["tombstones"]:
                return False
            if entry["date"] != today:
                # 历史日不可删（只读）：即便已入库，也拒绝删除。
                return False
            self.data["trash"][entry_id] = entry
            del self.data["entries"][entry_id]
            self.data["version"] += 1
            date = entry["date"]
            self.data["tombstones"][entry_id] = {
                "entry_id": entry_id,
                "deleted_by": deleted_by,
                "date": date,
                "v": self.data["version"],
                "ts": int(datetime.datetime.now().timestamp() * 1000),
                # 保留原条目的时间：占位符按它插回记录流的原位置，
                # 与服务端渲染/客户端原位替换保持严格一致（否则所有墓碑都堆到末尾）。
                "entry_ts": _as_int(entry.get("ts")),
            }
            self._save()
            self._changed.notify_all()
        self._render_dates({date})
        return True

    def latest_entry_for_date(self, ts_date: str) -> dict | None:
        """返回某日期（YYYY-MM-DD）内最新一条 entry，供 /d 删除。**仅今天可删**。

        ``ts_date != 今天`` 时返回 None（历史日只读，客户端不可删、服务端也不返回）。
        """
        self._maybe_seal_previous_day()
        today = today_utc8()
        if ts_date != today:
            return None
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
        """返回 version 在 (after_version, 当前] 内且 **date == 今天** 的条目与墓碑。

        只返回今天的增量（历史日以 ``Records/*.md`` 整文件为准，不做条目级对账）。
        条目按时间顺序（ts, entry_id）、墓碑按原条目时间（entry_ts，缺失则回退删除
        时间 ts）排序返回，而非按入库顺序（v）——使客户端按拉取顺序追加时也能得到
        时间有序的镜像，与其本地渲染/全量重建保持一致。
        """
        self._maybe_seal_previous_day()
        today = today_utc8()
        with self._lock:
            entries = [
                value
                for value in self.data["entries"].values()
                if value["date"] == today and value["v"] > after_version
            ]
            tombstones = [
                value
                for value in self.data["tombstones"].values()
                if value.get("date") == today and value["v"] > after_version
            ]
            return {
                "version": self.data["version"],
                "entries": sorted(
                    entries, key=lambda e: (int(e.get("ts", 0)), e["entry_id"])
                ),
                "tombstones": sorted(
                    tombstones,
                    key=lambda t: (int(t.get("entry_ts", t.get("ts", 0))), t["entry_id"]),
                ),
            }

    def wait_for_change(self, after_version: int, timeout: float = 25.0) -> dict:
        """等待新数据到来（长轮询），超时返回当前数据。"""
        with self._changed:
            if self.data["version"] > after_version:
                return self.pull(after_version)
            self._changed.wait(timeout)
            return self.pull(after_version)

    def render_records(self, records_dir: Path, trash_dir: Path) -> None:
        """把条目与 tombstone 渲染成 Records 文件（**只渲染今天**），并把已删正文渲染进垃圾桶。

        渲染会保留目标文件已有的 `<summary>`（由 AI 日总结写入）。渲染来源只有
        state.json，若直接重建会抹掉刚生成的总结，因此渲染前读取旧文件里
        的 summary 并在重写时带回。历史日以整文件为权威，绝不重排/重写。
        """
        self._maybe_seal_previous_day()
        self._write_day_files(records_dir, trash_dir, None)

    def _render_dates(self, dates: set[str]) -> None:
        """只重渲染指定日期的 Records/Trash 文件（供写后即时落盘）。"""
        if self.records_dir is None or not dates:
            return
        self._write_day_files(self.records_dir, self.trash_dir, set(dates))

    def rendered_version(self) -> int:
        """最后一次渲染所覆盖的权威版本（用于后台跳过无谓的全量重渲）。"""
        return int(getattr(self, "_rendered_version", 0))

    def _write_day_files(
        self,
        records_dir: Path,
        trash_dir: Path,
        targets: set[str] | None,
    ) -> None:
        """把权威状态渲染成 Records / Trash 文件。

        ``targets`` 为 None 时**只渲染今天**（全量维护/后台兜底）；历史日文件是权威
        文件（可能含旧格式），绝不整页重排。为日期集合时只渲染这些日期（即使最终为空
        也照写），供「写后即时落盘」与「日界封存」复用。
        """
        from . import render as render_mod

        records_dir.mkdir(parents=True, exist_ok=True)
        trash_dir.mkdir(parents=True, exist_ok=True)

        # 在 self._lock 内对权威状态做一次快照，避免渲染线程与 HTTP push/delete 并发
        # 迭代同一 dict 而触发 “dictionary changed size during iteration”。
        with self._lock:
            version = int(self.data.get("version", 0))
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

        today = today_utc8()
        if targets is None:
            # 只渲染今天：历史日整文件为准，不重排/重写。
            dates = [today]
        else:
            dates = sorted(set(targets))
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
                atomic_write(target, text)
        finally:
            if lock is not None:
                lock.release()
        if targets is None:
            trash_dates = [today] if today in trash_by_date else []
        else:
            trash_dates = sorted(set(targets) & set(trash_by_date))
        for date in trash_dates:
            trash_entries = trash_by_date.get(date, [])
            if not trash_entries:
                continue
            # 垃圾桶也按逐块 + 空行渲染，避免已删正文连成一行。
            blocks = "".join(
                render_mod.entry_block(e) + "\n"
                for e in sorted(
                    trash_entries, key=lambda e: (e["ts"], e["entry_id"])
                )
            )
            atomic_write(trash_dir / f"{date}.md", blocks)

        # 记录本次渲染覆盖到的权威版本，供后台据以跳过已被“写后即时渲染”覆盖的全量重渲。
        self._rendered_version = version

    def snapshot(self) -> dict:
        self._maybe_seal_previous_day()
        today = today_utc8()
        with self._lock:
            return {
                "version": self.data["version"],
                "entries": {
                    k: v for k, v in self.data["entries"].items() if v.get("date") == today
                },
                "tombstones": {
                    k: v for k, v in self.data["tombstones"].items() if v.get("date") == today
                },
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


def _normalized_date(value: object, ts: int) -> str:
    """把 entry 的 date 规范为合法的 YYYY-MM-DD；非法时回退到由 ts 推导。

    date 后续会被拼进文件名（<date>.md），禁止任何含路径分隔符/.. 的非日期值，
    否则渲染时会把文件写出数据目录之外（路径穿越）。
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
        datetime.datetime.fromtimestamp(
            ts / 1000.0, tz=_UTC8
        )
        .date()
        .isoformat()
    )