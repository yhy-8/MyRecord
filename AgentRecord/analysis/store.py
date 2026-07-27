"""SQLite persistence for disposable analysis runs and Agent audit artifacts.

The durable personal profile lives in ``AnalysisReports/Profile.md``.  SQLite
keeps only rebuildable runs, sources, validated-stage cache and telemetry.
"""

import datetime
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

from .. import settings


RUN_STATUSES = {"running", "completed", "failed"}
_SCHEMA_COLUMNS = {
    "analysis_runs": {
        "id", "kind", "period_start", "period_end", "origin", "trigger",
        "model_name", "status", "input_hash", "report_path", "error",
        "created_at", "completed_at",
    },
    "agent_artifacts": {
        "id", "run_id", "agent", "revision", "status", "payload_json",
        "error", "created_at",
    },
    "source_catalog": {
        "source_id", "relative_path", "source_date", "source_time",
        "record_index", "speaker", "tag", "content_hash", "excerpt",
        "last_seen_at",
    },
    "run_sources": {"run_id", "source_id"},
}
_LEGACY_PROFILE_COLUMNS = {
    "profile_entries": {
        "id", "run_id", "category", "title", "statement", "status",
        "confidence", "source_refs_json", "first_observed", "last_observed",
        "created_by", "supersedes_id", "created_at", "updated_at",
    },
    "profile_feedback": {
        "id", "entry_id", "action", "replacement_entry_id", "created_at",
    },
}
_LEGACY_SCHEMA_COLUMNS = {**_SCHEMA_COLUMNS, **_LEGACY_PROFILE_COLUMNS}


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: str) -> object:
    return json.loads(value) if value else {}


class AnalysisStore:
    """Transactional access to disposable analysis and audit state."""

    def __init__(self, path: Path | None = None):
        self.path = path or settings.ANALYSIS_DIR / ".analysis.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        # Inspect the physical schema before enabling WAL so an incompatible
        # database is rejected without modification. No schema version or
        # migration state is stored.
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                    """
                )
            }
            if tables:
                if tables == set(_LEGACY_SCHEMA_COLUMNS):
                    for table, expected_columns in _LEGACY_SCHEMA_COLUMNS.items():
                        actual_columns = {
                            row[1]
                            for row in connection.execute(
                                f"PRAGMA table_info({table})"
                            )
                        }
                        if actual_columns != expected_columns:
                            raise RuntimeError(
                                f"分析数据库表 {table} 结构不符合当前程序，"
                                "无法安全导出旧人物画像。"
                            )
                    self._migrate_legacy_profiles(connection)
                    tables = set(_SCHEMA_COLUMNS)
                elif tables != set(_SCHEMA_COLUMNS):
                    raise RuntimeError(
                        "分析数据库结构不符合当前程序。"
                        "只支持从上一版人物画像表安全迁移；请确认无需保留后，"
                        f"手动删除 {self.path} 及同名 -wal、-shm 文件再启动。"
                    )
                for table, expected_columns in _SCHEMA_COLUMNS.items():
                    actual_columns = {
                        row[1]
                        for row in connection.execute(
                            f"PRAGMA table_info({table})"
                        )
                    }
                    if actual_columns != expected_columns:
                        raise RuntimeError(
                            f"分析数据库表 {table} 结构不符合当前程序。"
                            "本项目不提供数据库迁移或兼容；请手动删除"
                            "数据库主文件及同名 -wal、-shm 文件再启动。"
                        )
        finally:
            connection.close()

        if tables:
            return

        with self.transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE analysis_runs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    report_path TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE agent_artifacts (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
                    agent TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, agent, revision)
                );

                CREATE TABLE source_catalog (
                    source_id TEXT PRIMARY KEY,
                    relative_path TEXT NOT NULL,
                    source_date TEXT NOT NULL,
                    source_time TEXT NOT NULL,
                    record_index INTEGER NOT NULL,
                    speaker TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    excerpt TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE TABLE run_sources (
                    run_id TEXT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
                    source_id TEXT NOT NULL REFERENCES source_catalog(source_id),
                    PRIMARY KEY(run_id, source_id)
                );

                CREATE INDEX idx_runs_period
                    ON analysis_runs(kind, period_start, period_end, origin, status);
                """
            )

    def _migrate_legacy_profiles(self, connection: sqlite3.Connection) -> None:
        """Back up, export active profile history to Markdown, then drop old tables."""
        backup_path = self.path.with_name(
            f"{self.path.stem}.pre-profile-markdown.sqlite3"
        )
        if not backup_path.exists():
            backup_connection = sqlite3.connect(backup_path)
            try:
                connection.backup(backup_connection)
            finally:
                backup_connection.close()

        entry_rows = connection.execute(
            """
            SELECT p.*, r.period_start, r.period_end
            FROM profile_entries AS p
            JOIN analysis_runs AS r ON r.id = p.run_id
            WHERE r.status = 'completed'
              AND (
                p.status IN ('accepted', 'superseded')
                OR p.created_by = 'user'
                OR EXISTS (
                    SELECT 1 FROM profile_feedback AS f
                    WHERE f.entry_id = p.id AND f.action = 'reject'
                )
              )
            ORDER BY p.created_at, p.id
            """
        ).fetchall()
        entries = []
        included_ids = set()
        for row in entry_rows:
            item = dict(row)
            try:
                refs = json.loads(item.pop("source_refs_json"))
            except (TypeError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    f"旧人物画像条目 {item.get('id', '')} 的来源无法解析"
                ) from error
            item["source_refs"] = refs
            item.pop("status", None)
            entries.append(item)
            included_ids.add(str(item["id"]))

        feedback = [
            dict(row)
            for row in connection.execute(
                """
                SELECT * FROM profile_feedback
                ORDER BY created_at, id
                """
            ).fetchall()
            if str(row["entry_id"]) in included_ids
            and (
                row["replacement_entry_id"] is None
                or str(row["replacement_entry_id"]) in included_ids
            )
        ]
        from .profile_store import ProfileStore

        ProfileStore(self.path.parent / "Profile.md").merge_legacy(
            entries, feedback
        )
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DROP TABLE profile_feedback")
            connection.execute("DROP TABLE profile_entries")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    def start_run(
        self,
        kind: str,
        period_start: str,
        period_end: str,
        origin: str,
        model_name: str,
        input_hash: str,
        *,
        trigger: str | None = None,
    ) -> str:
        if kind not in {"daily_profile", "daily_information", "weekly", "monthly"}:
            raise ValueError(f"不支持的分析类型: {kind}")
        if origin not in {"manual", "auto"}:
            raise ValueError(f"不支持的报告来源: {origin}")
        if kind in {"daily_profile", "daily_information"} and origin != "auto":
            raise ValueError("每日人物画像和信息简报只支持自动来源")
        trigger = trigger or ("manual" if origin == "manual" else "scheduled")
        if trigger not in {"manual", "scheduled", "retry"}:
            raise ValueError(f"不支持的触发方式: {trigger}")
        if (origin == "manual") != (trigger == "manual"):
            raise ValueError("手动来源只能使用 manual 触发，自动来源不能使用 manual 触发")
        run_id = uuid.uuid4().hex
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO analysis_runs(
                    id, kind, period_start, period_end, origin, trigger, model_name,
                    status, input_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
                """,
                (
                    run_id,
                    kind,
                    period_start,
                    period_end,
                    origin,
                    trigger,
                    model_name,
                    input_hash,
                    _now(),
                ),
            )
        return run_id

    def complete_run(self, run_id: str, report_path: Path | None = None) -> None:
        self._finish_run(
            run_id,
            "completed",
            report_path=str(report_path) if report_path is not None else None,
        )

    def fail_run(self, run_id: str, error: str) -> None:
        self._finish_run(run_id, "failed", error=error)

    def _finish_run(
        self,
        run_id: str,
        status: str,
        *,
        report_path: str | None = None,
        error: str | None = None,
    ) -> None:
        if status not in RUN_STATUSES:
            raise ValueError(f"无效运行状态: {status}")
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE analysis_runs
                SET status = ?, report_path = ?, error = ?, completed_at = ?
                WHERE id = ?
                """,
                (status, report_path, error, _now(), run_id),
            )

    def save_artifact(
        self,
        run_id: str,
        agent: str,
        payload: dict,
        *,
        status: str = "completed",
        error: str | None = None,
    ) -> str:
        artifact_id = uuid.uuid4().hex
        with self.transaction() as connection:
            revision = connection.execute(
                """
                SELECT COALESCE(MAX(revision), 0) + 1
                FROM agent_artifacts WHERE run_id = ? AND agent = ?
                """,
                (run_id, agent),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO agent_artifacts(
                    id, run_id, agent, revision, status, payload_json, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    run_id,
                    agent,
                    revision,
                    status,
                    _json(payload),
                    error,
                    _now(),
                ),
            )
        return artifact_id

    @staticmethod
    def has_completed_run(
        kind: str,
        period_start: str,
        period_end: str,
        origin: str = "auto",
        *,
        path: Path | None = None,
    ) -> bool:
        database_path = path or settings.ANALYSIS_DIR / ".analysis.sqlite3"
        if not database_path.is_file():
            return False
        try:
            connection = sqlite3.connect(database_path, timeout=10)
            try:
                row = connection.execute(
                    """
                    SELECT 1 FROM analysis_runs
                    WHERE kind = ? AND period_start = ? AND period_end = ?
                      AND origin = ? AND status = 'completed'
                    LIMIT 1
                    """,
                    (kind, period_start, period_end, origin),
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.DatabaseError:
            return False
        return row is not None

    def reusable_artifact(
        self,
        input_hash: str,
        kind: str,
        period_start: str,
        period_end: str,
        origin: str,
        model_name: str,
        agent: str,
    ) -> tuple[str, dict] | None:
        """Return the latest fully validated stage from an equivalent failed run."""
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT a.run_id, a.payload_json
                FROM agent_artifacts AS a
                JOIN analysis_runs AS r ON r.id = a.run_id
                WHERE r.input_hash = ? AND r.kind = ?
                  AND r.period_start = ? AND r.period_end = ?
                  AND r.origin = ? AND r.model_name = ?
                  AND r.status = 'failed'
                  AND a.agent = ? AND a.status = 'completed'
                ORDER BY r.completed_at DESC, a.revision DESC
                LIMIT 1
                """,
                (
                    input_hash,
                    kind,
                    period_start,
                    period_end,
                    origin,
                    model_name,
                    agent,
                ),
            ).fetchone()
        finally:
            connection.close()
        if not row:
            return None
        payload = _loads(row["payload_json"])
        return (row["run_id"], payload) if isinstance(payload, dict) else None

    def save_sources(self, run_id: str, records: Sequence[dict]) -> None:
        now = _now()
        with self.transaction() as connection:
            for record in records:
                text = str(record.get("text", ""))
                content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                values = (
                    record["source_id"],
                    record["path"],
                    record["date"],
                    record["time"],
                    int(record["record_index"]),
                    record.get("speaker", "user"),
                    record.get("tag", ""),
                    content_hash,
                    text[:500],
                    now,
                )
                existing = connection.execute(
                    """
                    SELECT relative_path, source_date, source_time, record_index,
                           speaker, tag, content_hash
                    FROM source_catalog WHERE source_id = ?
                    """,
                    (record["source_id"],),
                ).fetchone()
                immutable_values = (
                    record["path"],
                    record["date"],
                    record["time"],
                    int(record["record_index"]),
                    record.get("speaker", "user"),
                    record.get("tag", ""),
                    content_hash,
                )
                if existing and tuple(existing) != immutable_values:
                    raise RuntimeError(
                        f"来源 ID {record['source_id']} 已指向不同记录，拒绝覆写历史证据"
                    )
                connection.execute(
                    """
                    INSERT INTO source_catalog(
                        source_id, relative_path, source_date, source_time,
                        record_index, speaker, tag, content_hash, excerpt, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id) DO UPDATE SET
                        last_seen_at=excluded.last_seen_at
                    """,
                    values,
                )
                connection.execute(
                    "INSERT OR IGNORE INTO run_sources(run_id, source_id) VALUES (?, ?)",
                    (run_id, record["source_id"]),
                )

    def source_records(self, source_ids: Sequence[str]) -> list[dict]:
        if not source_ids:
            return []
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM source_catalog WHERE source_id IN (%s) "
                "ORDER BY source_date, source_time, record_index"
                % ",".join("?" for _ in source_ids),
                list(source_ids),
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]
