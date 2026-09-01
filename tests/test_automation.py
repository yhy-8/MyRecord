"""自动任务调度单元测试（不触发真实 AI/网络）。

覆盖新机制：每 15 分钟缺失检测、失败后 30 分钟重试、按任务各自的重试次数上限
（默认 2）停止、手动 /retry 直接重试全部失败任务（无顺序依赖）。
"""

import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.ai import settings
from server.ai.analysis import automation


class AutomationBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.original_diary = settings.DIARY_DIR
        self.original_analysis = settings.ANALYSIS_DIR
        self.original_config = settings.CONFIG
        self.original_run_generation = automation._run_generation
        self.original_dt_class = automation.datetime.datetime
        settings.DIARY_DIR = self.root / "Records"
        settings.ANALYSIS_DIR = self.root / "AnalysisReports"
        settings.DIARY_DIR.mkdir(parents=True, exist_ok=True)
        settings.ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
        settings.CONFIG = {
            "automation": {
                "enabled": True,
                "daily_summary": True,
                "weekly_report": True,
                "monthly_report": True,
            },
            "models": [
                {
                    "name": "mock",
                    "model_id": "mock-id",
                    "api_url": "https://example.test/v1",
                    "json_mode": False,
                    "temperature": 0.2,
                    "api_key": "secret",
                }
            ],
            "retry": {},
        }
        # 只替换 datetime.datetime 类（固定 now），保留 datetime.date/timedelta
        AutomationBase._FixedDateTime._now = datetime.datetime(2026, 7, 15, 10, 0)  # 周三
        automation.datetime.datetime = AutomationBase._FixedDateTime

    def tearDown(self):
        automation.datetime.datetime = self.original_dt_class
        settings.DIARY_DIR = self.original_diary
        settings.ANALYSIS_DIR = self.original_analysis
        settings.CONFIG = self.original_config
        automation._run_generation = self.original_run_generation
        self.tmp.cleanup()

    class _FixedDateTime(datetime.datetime):
        """固定 datetime.now() 的 datetime 子类（保留 fromisoformat 等真实行为）。"""

        _now = datetime.datetime(2026, 7, 15, 10, 0)

        @classmethod
        def now(cls, tz=None):
            return cls._now

    @staticmethod
    def _now() -> datetime.datetime:
        return AutomationBase._FixedDateTime._now

    @classmethod
    def _set_now(cls, value: datetime.datetime):
        cls._FixedDateTime._now = value

    @staticmethod
    def _state() -> dict:
        return {}

    @staticmethod
    def _record(state: dict, task: str) -> dict:
        return state.setdefault("tasks", {}).setdefault(task, {})

    def _diary(self, date: str, summary: str = "暂无今日总结。") -> Path:
        path = settings.DIARY_DIR / f"{date}.md"
        path.write_text(
            f"# {date}\n\n<summary>\n{summary}\n</summary>\n\n---\n\n**09:00:** 内容",
            encoding="utf-8",
        )
        return path


class PeriodTargetTests(AutomationBase):
    def test_latest_week_period_is_previous_complete_week(self):
        # 2026-07-15 是周三，上一完整自然周为 7/6(周一)-7/12(周日)
        start, end = automation._latest_week_period(datetime.date(2026, 7, 15))
        self.assertEqual("2026-07-06", start.isoformat())
        self.assertEqual("2026-07-12", end.isoformat())

    def test_latest_month_period_is_previous_complete_month(self):
        start, end = automation._latest_month_period(datetime.date(2026, 7, 15))
        self.assertEqual("2026-06-01", start.isoformat())
        self.assertEqual("2026-06-30", end.isoformat())

    def test_default_target_computes_missing_period_by_current_time(self):
        now = self._now()  # 昨天 07-14；上一周 07-06..07-12；上一月 06-01..06-30
        self.assertEqual({"start": "2026-07-14", "end": "2026-07-14"},
                         automation._default_task_target("daily_summary", now))
        self.assertEqual({"start": "2026-07-06", "end": "2026-07-12"},
                         automation._default_task_target("weekly_report", now))
        self.assertEqual({"start": "2026-06-01", "end": "2026-06-30"},
                         automation._default_task_target("monthly_report", now))


class MissingDetectionTests(AutomationBase):
    def test_daily_summary_missing_depends_on_file_and_summary(self):
        now = self._now()  # 昨天 = 2026-07-14
        self.assertFalse(automation._task_missing("daily_summary", now))
        self._diary("2026-07-14", "暂无今日总结。")
        self.assertTrue(automation._task_missing("daily_summary", now))
        self._diary("2026-07-14", "已完成")
        self.assertFalse(automation._task_missing("daily_summary", now))

    def test_weekly_monthly_missing_depends_on_period_and_report_file(self):
        now = self._now()  # 上一周 07-06..07-12，上一月 06-01..06-30
        self.assertFalse(automation._task_missing("weekly_report", now))
        self._diary("2026-07-08")
        self.assertTrue(automation._task_missing("weekly_report", now))
        report = settings.ANALYSIS_DIR / "Weekly" / "2026-07-06_to_2026-07-12.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("# 周报", encoding="utf-8")
        self.assertFalse(automation._task_missing("weekly_report", now))

        self.assertFalse(automation._task_missing("monthly_report", now))
        self._diary("2026-06-10")
        self.assertTrue(automation._task_missing("monthly_report", now))

    def test_scan_marks_newly_missing_as_due_and_present_as_ok(self):
        state = self._state()
        now = self._now()
        self._diary("2026-07-14")  # 昨天总结缺失
        automation._scan_missing(state, now, settings.CONFIG["automation"])
        daily = self._record(state, "daily_summary")
        self.assertEqual("pending", daily["status"])
        self.assertTrue(automation._retry_due(daily, now))
        self.assertEqual("ok", self._record(state, "weekly_report")["status"])
        self.assertEqual("ok", self._record(state, "monthly_report")["status"])

    def test_scan_resets_a_present_task_back_to_ok(self):
        state = self._state()
        now = self._now()
        self._diary("2026-07-14", "已完成")
        automation._scan_missing(state, now, settings.CONFIG["automation"])
        self.assertEqual("ok", self._record(state, "daily_summary")["status"])


class RetryStateTests(AutomationBase):
    def test_failure_increments_attempts_and_schedules_30min_retry(self):
        record = {"status": "pending", "attempts": 0, "error": "", "next_retry_at": ""}
        now = self._now()
        automation._mark_failure(record, "daily_summary", "模型失败", now)
        self.assertEqual("failed", record["status"])
        self.assertEqual(1, record["attempts"])
        self.assertEqual("模型失败", record["error"])
        expected = now + datetime.timedelta(minutes=30)
        self.assertEqual(expected,
                         datetime.datetime.fromisoformat(record["next_retry_at"]))
        self.assertFalse(automation._retry_due(record, now))
        self.assertTrue(automation._retry_due(record, expected))

    def test_failure_stops_when_retry_limit_is_zero(self):
        # 重试上限 0 → 第一次失败即停止
        with patch.dict(settings.CONFIG, {"retry": {"daily_summary_retry_limit": 0}}):
            record = {"status": "pending", "attempts": 0, "error": "", "next_retry_at": ""}
            automation._mark_failure(record, "daily_summary", "失败", self._now())
            self.assertEqual("blocked", record["status"])
            self.assertEqual("", record["next_retry_at"])

    def test_failure_retries_twice_then_blocks_with_default(self):
        record = {"status": "pending", "attempts": 0, "error": "", "next_retry_at": ""}
        now = self._now()
        automation._mark_failure(record, "weekly_report", "失败1", now)
        self.assertEqual("failed", record["status"])
        automation._mark_failure(record, "weekly_report", "失败2", now)
        self.assertEqual("failed", record["status"])
        automation._mark_failure(record, "weekly_report", "失败3", now)
        self.assertEqual("blocked", record["status"])  # 默认上限 2 → 第三次后停
        self.assertEqual("", record["next_retry_at"])


class RunAndRetryTests(AutomationBase):
    def test_run_due_returns_immediately_when_automation_disabled(self):
        settings.CONFIG["automation"] = {"enabled": False}
        automation.run_due_automatic_tasks()
        self.assertFalse((settings.ANALYSIS_DIR / ".automation-state.json").exists())

    def test_successful_generation_marks_task_ok(self):
        self._diary("2026-07-14")
        with patch.object(
            automation, "_run_generation", return_value=("总结", True)
        ) as generate:
            automation.run_due_automatic_tasks()
        generate.assert_called()
        state = automation._load_automation_state()
        self.assertEqual("ok", self._record(state, "daily_summary")["status"])

    def test_failed_generation_retries_after_30min_then_blocks(self):
        self._diary("2026-07-14")
        with patch.object(
            automation, "_run_generation", return_value=("模型失败", False)
        ) as generate:
            automation.run_due_automatic_tasks()  # 第一次：失败
        generate.assert_called_once()
        record = self._record(automation._load_automation_state(), "daily_summary")
        self.assertEqual("failed", record["status"])
        self.assertEqual(1, record["attempts"])
        retry_at = datetime.datetime.fromisoformat(record["next_retry_at"])

        # 未到重试时间（30 分钟内）不再生成
        self._set_now(retry_at - datetime.timedelta(minutes=5))
        with patch.object(
            automation, "_run_generation", side_effect=AssertionError("不应生成")
        ) as generate:
            automation.run_due_automatic_tasks()
        generate.assert_not_called()
        record = self._record(automation._load_automation_state(), "daily_summary")
        self.assertEqual(1, record["attempts"])

        # 到重试时间 → 再失败一次（attempts=2）
        self._set_now(retry_at)
        with patch.object(
            automation, "_run_generation", return_value=("仍失败", False)
        ):
            automation.run_due_automatic_tasks()
        record = self._record(automation._load_automation_state(), "daily_summary")
        self.assertEqual(2, record["attempts"])

        # 再次到重试时间 → 第三次失败 → 达到上限 blocked
        self._set_now(retry_at + datetime.timedelta(minutes=30))
        with patch.object(
            automation, "_run_generation", return_value=("仍失败", False)
        ):
            automation.run_due_automatic_tasks()
        record = self._record(automation._load_automation_state(), "daily_summary")
        self.assertEqual("blocked", record["status"])
        self.assertEqual("", record["next_retry_at"])

    def test_manual_retry_resets_and_retries_all_failed_tasks(self):
        self._diary("2026-07-14")
        with patch.object(
            automation, "_run_generation", return_value=("模型失败", False)
        ):
            automation.run_due_automatic_tasks()
        self.assertEqual(
            [("daily_summary", "日总结", "模型失败")],
            automation.failed_automatic_tasks(),
        )
        # /retry：重置计数、立即重试全部失败任务
        with patch.object(
            automation, "_run_generation", return_value=("总结成功", True)
        ) as generate:
            message, ok = automation.retry_failed_automatic_tasks()
        self.assertTrue(ok)
        self.assertIn("重试成功", message)
        generate.assert_called_once()
        record = self._record(automation._load_automation_state(), "daily_summary")
        self.assertEqual("ok", record["status"])
        self.assertEqual([], automation.failed_automatic_tasks())

    def test_retry_returns_no_failed_when_nothing_to_retry(self):
        message, ok = automation.retry_failed_automatic_tasks()
        self.assertTrue(ok)
        self.assertIn("没有失败", message)

    def test_status_snapshot_reports_task_states(self):
        self._diary("2026-07-14")
        with patch.object(
            automation, "_run_generation", return_value=("失败", False)
        ):
            automation.run_due_automatic_tasks()
        snapshot = automation.automation_status_snapshot()
        self.assertEqual("failed", snapshot["tasks"]["daily_summary"]["status"])
        self.assertIn("last_detection_at", snapshot)


if __name__ == "__main__":
    unittest.main()