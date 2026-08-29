"""自动任务调度逻辑单元测试（不触发真实 AI/网络）。

覆盖 automation.py 中纯调度逻辑：周期计算、目标校验/排队、缺失检测、
内容指纹、错误登记/清除、失败列表与状态快照，以及 automation=disabled 时
run_due_automatic_tasks 的快速返回。
"""

import datetime
import json
import tempfile
import unittest
from pathlib import Path

from server.ai import settings
from server.ai.analysis import automation


def _fixed_now() -> datetime.datetime:
    return datetime.datetime(2026, 7, 15, 10, 0)  # 2026-07-15 是周三


class AutomationSchedulingBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.original_diary = settings.DIARY_DIR
        self.original_analysis = settings.ANALYSIS_DIR
        self.original_config = settings.CONFIG
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

    def tearDown(self):
        settings.DIARY_DIR = self.original_diary
        settings.ANALYSIS_DIR = self.original_analysis
        settings.CONFIG = self.original_config
        self.tmp.cleanup()

    def _diary(self, date: str, summary: str = "暂无今日总结。") -> Path:
        path = settings.DIARY_DIR / f"{date}.md"
        body = "**09:00:** 内容\n" if summary == "暂无今日总结。" else "**09:00:** 内容\n"
        path.write_text(
            f"# {date}\n\n<summary>\n{summary}\n</summary>\n\n---\n\n{body}",
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _state() -> dict:
        return {}


class PeriodTargetTests(AutomationSchedulingBase):
    def test_latest_week_period_is_previous_complete_week(self):
        # 2026-07-15 是周三，上一完整自然周为 7/6(周一)-7/12(周日)
        start, end = automation._latest_week_period(datetime.date(2026, 7, 15))
        self.assertEqual("2026-07-06", start.isoformat())
        self.assertEqual("2026-07-12", end.isoformat())

    def test_latest_month_period_is_previous_complete_month(self):
        start, end = automation._latest_month_period(datetime.date(2026, 7, 15))
        self.assertEqual("2026-06-01", start.isoformat())
        self.assertEqual("2026-06-30", end.isoformat())

    def test_normalized_target_accepts_and_rejects_by_kind(self):
        # daily：开始==结束
        self.assertIsNotNone(
            automation._normalized_task_target(
                "daily_summary", {"start": "2026-07-14", "end": "2026-07-14"}
            )
        )
        self.assertIsNone(
            automation._normalized_task_target(
                "daily_summary", {"start": "2026-07-14", "end": "2026-07-15"}
            )
        )
        # weekly：周一开头、正好 7 天
        self.assertIsNotNone(
            automation._normalized_task_target(
                "weekly_report",
                {"start": "2026-07-13", "end": "2026-07-19"},
            )
        )
        self.assertIsNone(
            automation._normalized_task_target(
                "weekly_report",
                {"start": "2026-07-14", "end": "2026-07-19"},
            )
        )
        # monthly：1 号开头、到该月最后一天
        self.assertIsNotNone(
            automation._normalized_task_target(
                "monthly_report",
                {"start": "2026-06-01", "end": "2026-06-30"},
            )
        )
        self.assertIsNone(
            automation._normalized_task_target(
                "monthly_report",
                {"start": "2026-06-02", "end": "2026-06-30"},
            )
        )
        self.assertIsNone(automation._normalized_task_target("unknown", {"start": "a", "end": "b"}))

    def test_pending_targets_enqueue_dedupe_and_order(self):
        state = {}
        a = {"start": "2026-07-13", "end": "2026-07-19"}
        b = {"start": "2026-07-06", "end": "2026-07-12"}
        automation._enqueue_task_target(state, "weekly_report", b)
        automation._enqueue_task_target(state, "weekly_report", a)
        automation._enqueue_task_target(state, "weekly_report", a)  # 去重

        targets = automation._pending_task_targets(state, "weekly_report")
        self.assertEqual([b, a], targets)  # 按开始日期升序

        automation._dequeue_task_target(state, "weekly_report", a)
        self.assertEqual([b], automation._pending_task_targets(state, "weekly_report"))

        automation._clear_pending_task(state, "weekly_report")
        self.assertFalse(automation._has_pending_targets(state))


class MissingAndFailureTests(AutomationSchedulingBase):
    def test_default_task_target_for_daily_summary_is_yesterday(self):
        target = automation._default_task_target("daily_summary", _fixed_now())
        self.assertEqual({"start": "2026-07-14", "end": "2026-07-14"}, target)

    def test_daily_summary_missing_detection_and_artifact_status(self):
        now = _fixed_now()
        # 无日记文件 → 不算缺失
        self.assertFalse(
            automation._task_missing(
                "daily_summary", now, target={"start": "2026-07-14", "end": "2026-07-14"}
            )
        )
        # 有日记但总结未生成 → 缺失
        self._diary("2026-07-14", "暂无今日总结。")
        self.assertTrue(
            automation._task_missing(
                "daily_summary", now, target={"start": "2026-07-14", "end": "2026-07-14"}
            )
        )
        self.assertIn("缺失", automation._task_artifact_status("daily_summary", now))
        # 已生成总结 → 不再缺失
        self._diary("2026-07-14", "已完成总结")
        self.assertFalse(
            automation._task_missing(
                "daily_summary", now, target={"start": "2026-07-14", "end": "2026-07-14"}
            )
        )
        self.assertIn("已存在", automation._task_artifact_status("daily_summary", now))

    def test_content_failure_key_is_stable_and_blank_without_model(self):
        now = _fixed_now()
        self._diary("2026-07-14", "暂无今日总结。")
        target = {"start": "2026-07-14", "end": "2026-07-14"}
        k1 = automation._content_failure_key("daily_summary", now, target=target)
        k2 = automation._content_failure_key("daily_summary", now, target=target)
        self.assertTrue(k1)
        self.assertEqual(k1, k2)

        # 无模型配置 → 无法计算 → 返回空串（避免反复触发）
        settings.CONFIG["models"] = []
        self.assertEqual("", automation._content_failure_key("daily_summary", now, target=target))

    def test_set_and_clear_task_error_persists_failure_list(self):
        now = _fixed_now()
        self._diary("2026-07-14", "暂无今日总结。")
        target = {"start": "2026-07-14", "end": "2026-07-14"}
        state = {}
        automation._set_task_error(state, "daily_summary", "模型失败", target=target)
        self.assertIn("daily_summary", state["errors"])
        self.assertEqual("content", state["retry_kind"]["daily_summary"])
        self.assertTrue(automation._pending_task_targets(state, "daily_summary"))

        automation._save_automation_state(state)
        failures = automation.failed_automatic_tasks()
        self.assertEqual(1, len(failures))
        self.assertEqual("daily_summary", failures[0][0])
        self.assertEqual("日总结", failures[0][1])

        automation._clear_task_error(state, "daily_summary")
        self.assertNotIn("errors", state)
        self.assertNotIn("retry_after", state)
        self.assertNotIn("failure_targets", state)


class SnapshotAndRunTests(AutomationSchedulingBase):
    def test_snapshot_reports_retry_kind_and_pending(self):
        # status_snapshot 使用真实时钟（昨日记）
        today = datetime.date.today()
        yesterday = today - datetime.timedelta(days=1)
        self._diary(yesterday.isoformat(), "暂无今日总结。")
        target = {"start": yesterday.isoformat(), "end": yesterday.isoformat()}
        automation._enqueue_task_target(_state_ := {}, "daily_summary", target)
        automation._save_automation_state(_state_)

        snapshot = automation.automation_status_snapshot()
        self.assertIn("daily_summary", snapshot["pending_targets"])
        self.assertIn("缺失", snapshot["daily_summary_status"])

    def test_run_due_returns_immediately_when_automation_disabled(self):
        settings.CONFIG["automation"] = {"enabled": False}
        # 不抛异常、也不写任何状态文件
        automation.run_due_automatic_tasks()
        self.assertFalse((settings.ANALYSIS_DIR / ".automation-state.json").exists())


if __name__ == "__main__":
    unittest.main()