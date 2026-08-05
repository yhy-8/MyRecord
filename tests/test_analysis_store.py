import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from AgentRecord.analysis.store import AnalysisStore


class AnalysisStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "analysis.sqlite3"
        self.store = AnalysisStore(self.path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def start_run(self, *, trigger="manual", input_hash="hash"):
        return self.store.start_run(
            "weekly",
            "2026-07-06",
            "2026-07-12",
            "manual" if trigger == "manual" else "auto",
            "mock",
            input_hash,
            trigger=trigger,
        )

    def test_schema_only_keeps_runs_and_retry_artifacts(self):
        with closing(sqlite3.connect(self.path)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
        self.assertEqual({"analysis_runs", "agent_artifacts"}, tables)

    def test_manual_origin_cannot_use_automatic_trigger(self):
        with self.assertRaisesRegex(ValueError, "manual"):
            self.store.start_run(
                "weekly",
                "2026-07-13",
                "2026-07-19",
                "manual",
                "mock",
                "input-hash",
                trigger="retry",
            )

    def test_retired_daily_analysis_kinds_cannot_start_new_runs(self):
        for kind in ("daily_profile", "daily_information"):
            with self.subTest(kind=kind), self.assertRaisesRegex(
                ValueError, "不支持的分析类型"
            ):
                self.store.start_run(
                    kind,
                    "2026-07-15",
                    "2026-07-15",
                    "auto",
                    "mock",
                    "input-hash",
                    trigger="scheduled",
                )

    def test_completed_run_is_not_used_as_failed_retry_cache(self):
        run_id = self.start_run()
        self.store.save_artifact(run_id, "retrospective", {"markdown": "完成"})
        self.store.complete_run(run_id, Path("report.md"))

        self.assertIsNone(
            self.store.reusable_artifact(
                "hash",
                "weekly",
                "2026-07-06",
                "2026-07-12",
                "manual",
                "mock",
                "retrospective",
            )
        )

    def test_failed_run_exposes_latest_completed_stage_for_retry(self):
        run_id = self.start_run(input_hash="same")
        self.store.save_artifact(run_id, "retrospective", {"markdown": "已验证"})
        self.store.fail_run(run_id, "后续阶段失败")

        cached = self.store.reusable_artifact(
            "same",
            "weekly",
            "2026-07-06",
            "2026-07-12",
            "manual",
            "mock",
            "retrospective",
        )

        self.assertEqual(run_id, cached[0])
        self.assertEqual("已验证", cached[1]["markdown"])

    def test_model_usage_is_accumulated_in_memory(self):
        self.store.observe_telemetry(
            {
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "cached_tokens": 80,
                }
            }
        )
        self.store.observe_telemetry(
            {
                "usage": {
                    "prompt_tokens": 50,
                    "completion_tokens": 10,
                    "total_tokens": 60,
                }
            }
        )

        self.assertEqual(
            {
                "prompt_tokens": 150,
                "completion_tokens": 30,
                "total_tokens": 180,
                "cached_tokens": 80,
                "cache_miss_tokens": 0,
            },
            self.store.usage_totals(),
        )

    def test_incompatible_schema_is_left_untouched(self):
        legacy_path = Path(self.temp_dir.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.execute("CREATE TABLE legacy_nodes(id TEXT PRIMARY KEY)")
            connection.commit()
        original = legacy_path.read_bytes()

        with self.assertRaisesRegex(RuntimeError, "手动删除"):
            AnalysisStore(legacy_path)

        self.assertEqual(original, legacy_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
