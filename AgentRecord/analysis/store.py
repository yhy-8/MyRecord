"""SQLite persistence for disposable report runs and retryable Agent stages."""

import datetime
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

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
}
_USAGE_KEYS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cached_tokens",
    "cache_miss_tokens",
)


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
        self._usage = {key: 0 for key in _USAGE_KEYS}
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
        # The database is disposable. Reject unknown structures without
        # migrating or mutating them; deleting the file rebuilds a clean cache.
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
                if tables != set(_SCHEMA_COLUMNS):
                    raise RuntimeError(
                        "分析数据库结构不符合当前程序；该数据库只保存可重建缓存。"
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

                CREATE INDEX idx_runs_period
                    ON analysis_runs(kind, period_start, period_end, origin, status);
                """
            )

    def observe_telemetry(self, telemetry: dict) -> None:
        """Accumulate model usage for the report currently being generated."""
        usage = telemetry.get("usage", {}) if isinstance(telemetry, dict) else {}
        if not isinstance(usage, dict):
            return
        for key in _USAGE_KEYS:
            value = usage.get(key, 0)
            if isinstance(value, (int, float)) and value > 0:
                self._usage[key] += int(value)

    def usage_totals(self) -> dict[str, int]:
        return dict(self._usage)

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
        if kind not in {"weekly", "monthly"}:
            raise ValueError(f"不支持的分析类型: {kind}")
        if origin not in {"manual", "auto"}:
            raise ValueError(f"不支持的报告来源: {origin}")
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
