import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from AgentRecord.analysis.profile_store import ProfileStore
from AgentRecord.analysis.store import AnalysisStore


class AnalysisStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "analysis.sqlite3"
        self.store = AnalysisStore(self.path)
        self.profile_path = Path(self.temp_dir.name) / "Profile.md"
        self.profile = ProfileStore(self.profile_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def start_run(self, *, trigger="manual"):
        return self.store.start_run(
            "weekly",
            "2026-07-06",
            "2026-07-12",
            "manual" if trigger == "manual" else "auto",
            "mock",
            "hash",
            trigger=trigger,
        )

    def save_source(self, run_id, source_id="R-20260707-001", date="2026-07-07"):
        self.store.save_sources(
            run_id,
            [
                {
                    "source_id": source_id,
                    "path": f"{date}.md",
                    "date": date,
                    "time": "09:00",
                    "record_index": 1,
                    "speaker": "user",
                    "tag": "",
                    "text": "原始记录" * 200,
                }
            ],
        )

    @staticmethod
    def profile_entry(
        *,
        temp_id="p1",
        category="viewpoint",
        title="观点",
        statement="内容",
        source_ref="R-20260707-001",
        first_observed="2026-07-07",
        last_observed="2026-07-07",
        supersedes_id=None,
    ):
        return {
            "temp_id": temp_id,
            "category": category,
            "title": title,
            "statement": statement,
            "confidence": 0.8,
            "source_refs": [source_ref],
            "first_observed": first_observed,
            "last_observed": last_observed,
            "supersedes_id": supersedes_id,
        }

    def commit_profile(
        self,
        entry,
        *,
        run_id="run-1",
        period_start="2026-07-06",
        period_end="2026-07-12",
    ):
        ids, snapshot = self.profile.commit_entries(
            run_id=run_id,
            period_start=period_start,
            period_end=period_end,
            entries=[entry],
            decisions={entry["temp_id"]: "accepted"},
        )
        return ids[entry["temp_id"]], snapshot

    def test_new_schema_keeps_profile_out_of_sqlite(self):
        with closing(sqlite3.connect(self.path)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertEqual(0, version)
        self.assertNotIn("profile_entries", tables)
        self.assertNotIn("profile_feedback", tables)
        self.assertNotIn("knowledge_edges", tables)

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

    def test_daily_information_run_is_audited_without_schema_change(self):
        run_id = self.store.start_run(
            "daily_information",
            "2026-07-15",
            "2026-07-15",
            "auto",
            "mock",
            "input-hash",
            trigger="scheduled",
        )
        self.store.save_artifact(
            run_id,
            "daily_information_collector",
            {"highlights": [], "_telemetry": {"usage": {"total_tokens": 10}}},
        )
        self.store.complete_run(run_id, Path("Information/2026-07-15.md"))

        self.assertTrue(
            AnalysisStore.has_completed_run(
                "daily_information",
                "2026-07-15",
                "2026-07-15",
                path=self.path,
            )
        )

    def test_source_catalog_keeps_location_hash_and_excerpt(self):
        run_id = self.start_run()
        self.save_source(run_id)
        source = self.store.source_records(["R-20260707-001"])[0]
        self.assertEqual("2026-07-07.md", source["relative_path"])
        self.assertEqual(64, len(source["content_hash"]))
        self.assertEqual(500, len(source["excerpt"]))

    def test_source_catalog_rejects_reusing_an_id_for_different_content(self):
        run_id = self.start_run()
        self.save_source(run_id)

        with self.assertRaisesRegex(RuntimeError, "拒绝覆写历史证据"):
            self.store.save_sources(
                run_id,
                [
                    {
                        "source_id": "R-20260707-001",
                        "path": "2026-07-07.md",
                        "date": "2026-07-07",
                        "time": "09:00",
                        "record_index": 1,
                        "speaker": "user",
                        "tag": "",
                        "text": "后来变更的内容",
                    }
                ],
            )

        source = self.store.source_records(["R-20260707-001"])[0]
        self.assertIn("原始记录", source["excerpt"])

    def test_accepted_profile_revision_supersedes_previous(self):
        old_id, _ = self.commit_profile(
            self.profile_entry(
                category="principle", title="旧理念", statement="旧内容"
            )
        )
        new_id, _ = self.commit_profile(
            self.profile_entry(
                temp_id="p2",
                category="principle",
                title="新理念",
                statement="修订内容",
                source_ref="R-20260714-001",
                last_observed="2026-07-14",
                supersedes_id=old_id,
            ),
            run_id="run-2",
            period_start="2026-07-13",
            period_end="2026-07-19",
        )

        self.assertEqual(
            [new_id],
            [item["id"] for item in self.profile.active_profiles("2026-07-19")],
        )
        self.assertEqual(
            [old_id],
            [item["id"] for item in self.profile.active_profiles("2026-07-12")],
        )

    def test_profile_snapshot_can_be_restored_after_report_failure(self):
        old_id, _ = self.commit_profile(
            self.profile_entry(title="已交付观点", statement="旧内容")
        )
        _, snapshot = self.commit_profile(
            self.profile_entry(
                temp_id="p2",
                title="未交付修订",
                statement="新内容",
                source_ref="R-20260714-001",
                last_observed="2026-07-14",
                supersedes_id=old_id,
            ),
            run_id="failed-run",
            period_start="2026-07-13",
            period_end="2026-07-19",
        )
        self.profile.restore(snapshot)

        self.assertEqual(
            [old_id],
            [item["id"] for item in self.profile.active_profiles("2026-07-19")],
        )

    def test_profile_cutoff_blocks_future_information(self):
        self.commit_profile(
            self.profile_entry(
                category="interest",
                title="未来关注",
                statement="七月二十日才出现",
                source_ref="R-20260720-001",
                first_observed="2026-07-20",
                last_observed="2026-07-20",
            ),
            period_start="2026-07-20",
            period_end="2026-07-20",
        )
        self.assertEqual([], self.profile.active_profiles("2026-07-12"))

    def test_profile_cutoff_uses_originating_period_not_only_observation_date(self):
        self.commit_profile(
            self.profile_entry(
                title="后一周才得出的结论",
                statement="虽引用较早记录，但是后一周的分析结果。",
            ),
            period_start="2026-07-13",
            period_end="2026-07-19",
        )
        self.assertEqual([], self.profile.active_profiles("2026-07-12"))

    def test_future_feedback_does_not_rewrite_historical_profile_snapshot(self):
        entry_id, _ = self.commit_profile(
            self.profile_entry(title="原观点", statement="原内容")
        )
        with patch(
            "AgentRecord.analysis.profile_store._now",
            return_value="2026-07-20T09:00:00",
        ):
            replacement_id = self.profile.record_user_feedback(
                entry_id, "correct", title="修正观点", body="修正内容"
            )

        before = self.profile.active_profiles("2026-07-19")
        after = self.profile.active_profiles("2026-07-20")
        self.assertEqual([entry_id], [item["id"] for item in before])
        self.assertEqual([replacement_id], [item["id"] for item in after])

    def test_stale_profile_feedback_is_rejected(self):
        entry_id, _ = self.commit_profile(
            self.profile_entry(
                category="interest", title="关注领域", statement="持续关注。"
            )
        )
        self.profile.record_user_feedback(entry_id, "reject")

        with self.assertRaisesRegex(ValueError, "已不是可反馈状态"):
            self.profile.record_user_feedback(entry_id, "accept")

    def test_markdown_profile_and_feedback_are_readable_and_auditable(self):
        entry_id, _ = self.commit_profile(
            self.profile_entry(title="原观点", statement="原内容")
        )
        replacement = self.profile.record_user_feedback(
            entry_id, "correct", title="修正观点", body="修正内容"
        )

        candidates = self.profile.feedback_candidates()
        self.assertEqual(replacement, candidates[0]["id"])
        self.assertEqual("修正观点", candidates[0]["title"])
        content = self.profile_path.read_text(encoding="utf-8")
        self.assertTrue(content.startswith("---\n"))
        self.assertIn("format: agentrecord-profile-v1", content)
        self.assertIn("# 人物画像", content)
        self.assertIn("修正观点", content)
        self.assertIn("action: correct", content)

    def test_legacy_profile_tables_are_backed_up_and_migrated_to_markdown(self):
        run_id = self.start_run()
        self.store.complete_run(run_id, Path("report.md"))
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript(
                """
                CREATE TABLE profile_entries (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    status TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    source_refs_json TEXT NOT NULL,
                    first_observed TEXT NOT NULL,
                    last_observed TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    supersedes_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE profile_feedback (
                    id TEXT PRIMARY KEY,
                    entry_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    replacement_entry_id TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT INTO profile_entries VALUES (
                    'legacy-1', ?, 'principle', '旧画像', '保留这条原则',
                    'accepted', 0.9, '["R-20260707-001"]',
                    '2026-07-07', '2026-07-07', 'retrospective', NULL,
                    '2026-07-12T10:00:00', '2026-07-12T10:00:00'
                )
                """,
                (run_id,),
            )
            connection.commit()

        AnalysisStore(self.path)

        self.assertEqual(
            ["legacy-1"],
            [item["id"] for item in self.profile.active_profiles("2026-07-12")],
        )
        backup = self.path.with_name(
            f"{self.path.stem}.pre-profile-markdown.sqlite3"
        )
        self.assertTrue(backup.exists())
        with closing(sqlite3.connect(self.path)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertNotIn("profile_entries", tables)
        with closing(sqlite3.connect(backup)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM profile_entries"
            ).fetchone()[0]
        self.assertEqual(1, count)

    def test_incompatible_schema_fails_without_replacing_database(self):
        legacy_path = Path(self.temp_dir.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.execute("CREATE TABLE legacy_nodes(id TEXT PRIMARY KEY)")
            connection.commit()
        original = legacy_path.read_bytes()

        with self.assertRaisesRegex(RuntimeError, "只支持从上一版人物画像表"):
            AnalysisStore(legacy_path)

        self.assertEqual(original, legacy_path.read_bytes())
        self.assertFalse(Path(f"{legacy_path}.legacy.bak").exists())


if __name__ == "__main__":
    unittest.main()
