"""Markdown-backed, versioned personal profile storage.

AI agents only propose semantic profile entries.  This module owns IDs,
history, feedback events, Markdown rendering and atomic persistence.
"""

from __future__ import annotations

import datetime
import json
import re
import uuid
from pathlib import Path
from typing import Sequence

import yaml

from .. import settings
from ..file_lock import FileLock


PROFILE_CATEGORIES = {
    "viewpoint",
    "principle",
    "ideal",
    "behavior_pattern",
    "interest",
}
PROFILE_PATH_NAME = "Profile.md"
PROFILE_FORMAT = "agentrecord-profile-v1"
_CATEGORY_LABELS = {
    "viewpoint": "观点",
    "principle": "理念",
    "ideal": "理想",
    "behavior_pattern": "行为模式",
    "interest": "关注领域",
}


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _date_part(value: object) -> str:
    return str(value or "")[:10]


def _plain_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _empty_data() -> dict:
    return {
        "format": PROFILE_FORMAT,
        "updated_at": "",
        "entries": [],
        "feedback": [],
    }


class ProfileStore:
    """Read and atomically update the durable Markdown personal profile."""

    def __init__(self, path: Path | None = None):
        self.path = path or settings.ANALYSIS_DIR / PROFILE_PATH_NAME
        self.lock_path = self.path.parent / ".profile.lock"

    def _load(self) -> dict:
        if not self.path.exists():
            return _empty_data()
        content = self.path.read_text(encoding="utf-8")
        if not content.startswith("---\n"):
            raise RuntimeError(f"人物画像文件缺少 YAML 前置区: {self.path}")
        boundary = content.find("\n---\n", 4)
        if boundary < 0:
            raise RuntimeError(f"人物画像文件 YAML 前置区未闭合: {self.path}")
        try:
            data = yaml.safe_load(content[4:boundary]) or {}
        except yaml.YAMLError as error:
            raise RuntimeError(f"人物画像文件无法解析: {error}") from error
        self._validate_data(data)
        return data

    @staticmethod
    def _validate_data(data: object) -> None:
        if not isinstance(data, dict) or data.get("format") != PROFILE_FORMAT:
            raise RuntimeError("人物画像文件格式不受当前程序支持")
        entries = data.get("entries", [])
        feedback = data.get("feedback", [])
        if not isinstance(entries, list) or not isinstance(feedback, list):
            raise RuntimeError("人物画像 entries 或 feedback 格式错误")

        entry_ids: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise RuntimeError("人物画像条目必须是对象")
            entry_id = str(entry.get("id", "")).strip()
            if not entry_id or entry_id in entry_ids:
                raise RuntimeError("人物画像条目 ID 为空或重复")
            if entry.get("category") not in PROFILE_CATEGORIES:
                raise RuntimeError(f"人物画像条目 {entry_id} 的 category 无效")
            if not _plain_text(entry.get("title")) or not _plain_text(
                entry.get("statement")
            ):
                raise RuntimeError(f"人物画像条目 {entry_id} 缺少标题或陈述")
            refs = entry.get("source_refs", [])
            if not isinstance(refs, list) or any(
                not isinstance(ref, str) or not ref for ref in refs
            ):
                raise RuntimeError(f"人物画像条目 {entry_id} 的来源格式错误")
            try:
                confidence = float(entry.get("confidence"))
            except (TypeError, ValueError) as error:
                raise RuntimeError(
                    f"人物画像条目 {entry_id} 的置信度无效"
                ) from error
            if not 0 <= confidence <= 1:
                raise RuntimeError(f"人物画像条目 {entry_id} 的置信度超出范围")
            for key in (
                "first_observed",
                "last_observed",
                "period_start",
                "period_end",
                "created_at",
                "updated_at",
            ):
                if not str(entry.get(key, "")).strip():
                    raise RuntimeError(f"人物画像条目 {entry_id} 缺少 {key}")
            try:
                first_observed = datetime.date.fromisoformat(
                    str(entry["first_observed"])
                )
                last_observed = datetime.date.fromisoformat(
                    str(entry["last_observed"])
                )
                period_start = datetime.date.fromisoformat(
                    str(entry["period_start"])
                )
                period_end = datetime.date.fromisoformat(str(entry["period_end"]))
                datetime.datetime.fromisoformat(str(entry["created_at"]))
                datetime.datetime.fromisoformat(str(entry["updated_at"]))
            except ValueError as error:
                raise RuntimeError(
                    f"人物画像条目 {entry_id} 的日期或时间格式无效"
                ) from error
            if first_observed > last_observed or period_start > period_end:
                raise RuntimeError(f"人物画像条目 {entry_id} 的日期区间倒置")
            if not str(entry.get("run_id", "")).strip():
                raise RuntimeError(f"人物画像条目 {entry_id} 缺少 run_id")
            if entry.get("created_by") not in {"retrospective", "user"}:
                raise RuntimeError(f"人物画像条目 {entry_id} 的 created_by 无效")
            entry_ids.add(entry_id)

        feedback_ids: set[str] = set()
        for event in feedback:
            if not isinstance(event, dict):
                raise RuntimeError("人物画像反馈必须是对象")
            event_id = str(event.get("id", "")).strip()
            entry_id = str(event.get("entry_id", "")).strip()
            action = str(event.get("action", "")).strip()
            if not event_id or event_id in feedback_ids:
                raise RuntimeError("人物画像反馈 ID 为空或重复")
            if entry_id not in entry_ids or action not in {
                "accept",
                "reject",
                "correct",
            }:
                raise RuntimeError(f"人物画像反馈 {event_id} 引用无效")
            replacement = event.get("replacement_entry_id")
            if replacement and str(replacement) not in entry_ids:
                raise RuntimeError(f"人物画像反馈 {event_id} 的替代条目无效")
            if not str(event.get("created_at", "")).strip():
                raise RuntimeError(f"人物画像反馈 {event_id} 缺少时间")
            try:
                datetime.datetime.fromisoformat(str(event["created_at"]))
            except ValueError as error:
                raise RuntimeError(
                    f"人物画像反馈 {event_id} 的时间格式无效"
                ) from error
            if action == "reject" and replacement:
                raise RuntimeError(f"人物画像反馈 {event_id} 的否决不应有替代条目")
            if action in {"accept", "correct"} and not replacement:
                raise RuntimeError(f"人物画像反馈 {event_id} 缺少替代条目")
            feedback_ids.add(event_id)

        supersedes = {
            str(entry["id"]): str(entry["supersedes_id"])
            for entry in entries
            if entry.get("supersedes_id")
        }
        for entry in entries:
            supersedes_id = entry.get("supersedes_id")
            if supersedes_id and str(supersedes_id) not in entry_ids:
                raise RuntimeError(
                    f"人物画像条目 {entry['id']} 尝试替代未知条目"
                )
            visited = {str(entry["id"])}
            cursor = str(supersedes_id or "")
            while cursor:
                if cursor in visited:
                    raise RuntimeError("人物画像替代关系包含循环")
                visited.add(cursor)
                cursor = supersedes.get(cursor, "")

        entries_by_id = {str(entry["id"]): entry for entry in entries}
        for event in feedback:
            replacement = event.get("replacement_entry_id")
            if replacement and str(
                entries_by_id[str(replacement)].get("supersedes_id") or ""
            ) != str(event["entry_id"]):
                raise RuntimeError(
                    f"人物画像反馈 {event['id']} 的替代链不一致"
                )

    @staticmethod
    def _effective_date(entry: dict) -> str:
        if entry.get("created_by") == "user":
            return _date_part(entry.get("created_at"))
        return _date_part(entry.get("period_end"))

    @classmethod
    def _active_entries(cls, data: dict, period_end: str) -> list[dict]:
        entries = data["entries"]
        active_candidates = [
            entry
            for entry in entries
            if _date_part(entry.get("last_observed")) <= period_end
            and cls._effective_date(entry) <= period_end
        ]
        superseded_ids = {
            str(entry["supersedes_id"])
            for entry in active_candidates
            if entry.get("supersedes_id")
        }
        rejected_ids = {
            str(event["entry_id"])
            for event in data["feedback"]
            if event.get("action") == "reject"
            and _date_part(event.get("created_at")) <= period_end
        }
        return [
            entry
            for entry in active_candidates
            if str(entry["id"]) not in superseded_ids
            and str(entry["id"]) not in rejected_ids
        ]

    @staticmethod
    def _public_entry(entry: dict) -> dict:
        item = dict(entry)
        item["source_refs"] = list(entry.get("source_refs", []))
        item["body"] = item["statement"]
        item["node_type"] = item["category"]
        return item

    def active_profiles(
        self, period_end: str, limit: int | None = None
    ) -> list[dict]:
        data = self._load()
        entries = self._active_entries(data, period_end)
        entries.sort(
            key=lambda entry: (
                str(entry.get("updated_at") or entry.get("created_at", "")),
                str(entry["id"]),
            ),
            reverse=True,
        )
        if limit is not None:
            entries = entries[:limit]
        return [self._public_entry(entry) for entry in entries]

    def feedback_candidates(self, limit: int = 20) -> list[dict]:
        return self.active_profiles(datetime.date.today().isoformat(), limit)

    def _render(self, data: dict) -> str:
        front_matter = yaml.safe_dump(
            data,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ).rstrip()
        current = self._active_entries(data, datetime.date.today().isoformat())
        current.sort(
            key=lambda entry: (
                entry["category"],
                _plain_text(entry["title"]).casefold(),
            )
        )
        current_ids = {str(entry["id"]) for entry in current}
        lines = [
            "---",
            front_matter,
            "---",
            "# 人物画像",
            "",
            "> 本文件是人物画像的权威存储。YAML 前置区用于可靠恢复，"
            "下方正文由程序同步生成，便于阅读和备份。",
            "",
            "## 当前画像",
        ]
        if not current:
            lines.extend(["", "当前没有已激活的人物画像条目。"])
        for category in (
            "viewpoint",
            "principle",
            "ideal",
            "behavior_pattern",
            "interest",
        ):
            category_entries = [
                entry for entry in current if entry["category"] == category
            ]
            if not category_entries:
                continue
            lines.extend(["", f"### {_CATEGORY_LABELS[category]}"])
            for entry in category_entries:
                refs = "、".join(f"[{ref}]" for ref in entry["source_refs"]) or "无"
                lines.extend(
                    [
                        "",
                        f"#### {_plain_text(entry['title'])}",
                        "",
                        _plain_text(entry["statement"]),
                        "",
                        f"- 置信度：{float(entry['confidence']):.2f}",
                        f"- 观察区间：{entry['first_observed']} 至 {entry['last_observed']}",
                        f"- 来源：{refs}",
                        f"- 条目 ID：`{entry['id']}`",
                    ]
                )

        historical = [
            entry for entry in data["entries"] if str(entry["id"]) not in current_ids
        ]
        lines.extend(["", "## 历史版本与反馈"])
        if not historical and not data["feedback"]:
            lines.extend(["", "暂无历史版本或用户反馈。"])
        for entry in sorted(
            historical, key=lambda item: str(item.get("created_at", "")), reverse=True
        ):
            lines.append(
                f"- `{entry['id']}` [{_CATEGORY_LABELS[entry['category']]}] "
                f"{_plain_text(entry['title'])}（创建于 {entry['created_at']}）"
            )
        for event in sorted(
            data["feedback"],
            key=lambda item: str(item.get("created_at", "")),
            reverse=True,
        ):
            replacement = (
                f"，替代条目 `{event['replacement_entry_id']}`"
                if event.get("replacement_entry_id")
                else ""
            )
            lines.append(
                f"- {event['created_at']}：对 `{event['entry_id']}` 执行 "
                f"`{event['action']}`{replacement}"
            )
        return "\n".join(lines).rstrip() + "\n"

    def _write(self, data: dict) -> None:
        data["updated_at"] = _now()
        self._validate_data(data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(
            self.path.suffix + f".{uuid.uuid4().hex}.tmp"
        )
        temp_path.write_text(self._render(data), encoding="utf-8")
        temp_path.replace(self.path)

    def commit_entries(
        self,
        *,
        run_id: str,
        period_start: str,
        period_end: str,
        entries: Sequence[dict],
        decisions: dict[str, str],
    ) -> tuple[dict[str, str], bytes | None]:
        """Commit Reviewer-accepted entries and return IDs plus rollback snapshot."""
        lock = FileLock.acquire(self.lock_path, blocking=True)
        if lock is None:  # pragma: no cover - blocking acquisition only fails unusually
            raise RuntimeError("无法获取人物画像文件锁")
        try:
            previous = self.path.read_bytes() if self.path.exists() else None
            data = self._load()
            current_ids = {
                str(entry["id"])
                for entry in self._active_entries(
                    data, datetime.date.today().isoformat()
                )
            }
            id_map: dict[str, str] = {}
            now = _now()
            for entry in entries:
                temp_id = str(entry["temp_id"])
                if decisions.get(temp_id) != "accepted":
                    continue
                supersedes_id = entry.get("supersedes_id") or None
                if supersedes_id and str(supersedes_id) not in current_ids:
                    raise RuntimeError(
                        "分析运行期间人物画像已被其他操作更新，本次候选不能覆盖新状态"
                    )
                entry_id = uuid.uuid4().hex
                data["entries"].append(
                    {
                        "id": entry_id,
                        "run_id": run_id,
                        "period_start": period_start,
                        "period_end": period_end,
                        "category": entry["category"],
                        "title": _plain_text(entry["title"]),
                        "statement": _plain_text(entry["statement"]),
                        "confidence": float(entry["confidence"]),
                        "source_refs": list(dict.fromkeys(entry["source_refs"])),
                        "first_observed": entry["first_observed"],
                        "last_observed": entry["last_observed"],
                        "created_by": "retrospective",
                        "supersedes_id": supersedes_id,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                id_map[temp_id] = entry_id
                if supersedes_id:
                    current_ids.discard(str(supersedes_id))
                current_ids.add(entry_id)
            self._write(data)
            return id_map, previous
        finally:
            lock.release()

    def restore(self, previous: bytes | None) -> None:
        lock = FileLock.acquire(self.lock_path, blocking=True)
        if lock is None:  # pragma: no cover
            raise RuntimeError("无法获取人物画像文件锁")
        try:
            if previous is None:
                self.path.unlink(missing_ok=True)
                return
            temp_path = self.path.with_suffix(
                self.path.suffix + f".{uuid.uuid4().hex}.restore.tmp"
            )
            temp_path.write_bytes(previous)
            temp_path.replace(self.path)
        finally:
            lock.release()

    def record_user_feedback(
        self,
        entry_id: str,
        action: str,
        *,
        title: str = "",
        body: str = "",
    ) -> str | None:
        if action not in {"accept", "reject", "correct"}:
            raise ValueError(f"未知反馈操作: {action}")
        lock = FileLock.acquire(self.lock_path, blocking=True)
        if lock is None:  # pragma: no cover
            raise RuntimeError("无法获取人物画像文件锁")
        try:
            data = self._load()
            active = {
                str(entry["id"]): entry
                for entry in self._active_entries(
                    data, datetime.date.today().isoformat()
                )
            }
            entry = active.get(entry_id)
            if not entry:
                raise ValueError(f"人物画像条目不存在或已不是可反馈状态: {entry_id}")
            now = _now()
            replacement_id = None
            if action in {"accept", "correct"}:
                replacement_id = uuid.uuid4().hex
                data["entries"].append(
                    {
                        **entry,
                        "id": replacement_id,
                        "title": _plain_text(title) or entry["title"],
                        "statement": _plain_text(body) or entry["statement"],
                        "confidence": 1.0,
                        "created_by": "user",
                        "supersedes_id": entry_id,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
            data["feedback"].append(
                {
                    "id": uuid.uuid4().hex,
                    "entry_id": entry_id,
                    "action": action,
                    "replacement_entry_id": replacement_id,
                    "created_at": now,
                }
            )
            self._write(data)
            return replacement_id
        finally:
            lock.release()

    def merge_legacy(self, entries: Sequence[dict], feedback: Sequence[dict]) -> None:
        """Merge a validated legacy SQLite export without overwriting Markdown."""
        lock = FileLock.acquire(self.lock_path, blocking=True)
        if lock is None:  # pragma: no cover
            raise RuntimeError("无法获取人物画像文件锁")
        try:
            data = self._load()
            existing_entries = {str(item["id"]): item for item in data["entries"]}
            for entry in entries:
                entry_id = str(entry["id"])
                normalized = {
                    "id": entry_id,
                    "run_id": str(entry["run_id"]),
                    "period_start": str(entry["period_start"]),
                    "period_end": str(entry["period_end"]),
                    "category": str(entry["category"]),
                    "title": _plain_text(entry["title"]),
                    "statement": _plain_text(entry["statement"]),
                    "confidence": float(entry["confidence"]),
                    "source_refs": list(entry["source_refs"]),
                    "first_observed": str(entry["first_observed"]),
                    "last_observed": str(entry["last_observed"]),
                    "created_by": str(entry["created_by"]),
                    "supersedes_id": entry.get("supersedes_id") or None,
                    "created_at": str(entry["created_at"]),
                    "updated_at": str(entry["updated_at"]),
                }
                existing = existing_entries.get(entry_id)
                if existing and json.dumps(existing, sort_keys=True) != json.dumps(
                    normalized, sort_keys=True
                ):
                    raise RuntimeError(
                        f"Markdown 中的人物画像条目 {entry_id} 与旧数据库冲突"
                    )
                if not existing:
                    data["entries"].append(normalized)
                    existing_entries[entry_id] = normalized

            existing_feedback = {
                str(item["id"]): item for item in data["feedback"]
            }
            for event in feedback:
                event_id = str(event["id"])
                normalized = {
                    "id": event_id,
                    "entry_id": str(event["entry_id"]),
                    "action": str(event["action"]),
                    "replacement_entry_id": event.get("replacement_entry_id") or None,
                    "created_at": str(event["created_at"]),
                }
                existing = existing_feedback.get(event_id)
                if existing and json.dumps(existing, sort_keys=True) != json.dumps(
                    normalized, sort_keys=True
                ):
                    raise RuntimeError(
                        f"Markdown 中的人物画像反馈 {event_id} 与旧数据库冲突"
                    )
                if not existing:
                    data["feedback"].append(normalized)
                    existing_feedback[event_id] = normalized
            self._write(data)
        finally:
            lock.release()
