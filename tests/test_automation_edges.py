"""自动任务调度的补充边界测试（不触发真实 AI/网络）。

聚焦现有套件未覆盖的判定分支：
- _model_ready 各种不可用原因（get_model 抛错 / api_url 空 / api_key 空）
- _run_generation 的 weekly/monthly 分发
- _retry_due / _detection_due 对畸形时间戳的容错
- 报告锁忙（busy）的推迟：保持 pending、不累计次数
- _process_due 对 blocked / 非陈旧 running 的跳过
- retry 在停用、锁忙、异常下的返回
- automation 配置非法（非 dict）时的自我保护
"""

import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.ai import settings
from server.ai.analysis import automation


class AutomationEdgeBase(unittest.TestCase):
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
            "automation": {"enabled": True, "daily_summary": True,
                           "weekly_report": True, "monthly_report": True},
            "models": [{"name": "mock", "model_id": "mock-id",
                        "api_url": "https://example.test/v1",
                        "api_key": "secret"}],
            "retry": {},
        }
        AutomationEdgeBase._Fixed._now = datetime.datetime(2026, 7, 15, 10, 0)
        automation.datetime.datetime = AutomationEdgeBase._Fixed

    def tearDown(self):
        automation.datetime.datetime = self.original_dt_class
        settings.DIARY_DIR = self.original_diary
        settings.ANALYSIS_DIR = self.original_analysis
        settings.CONFIG = self.original_config
        automation._run_generation = self.original_run_generation
        self.tmp.cleanup()

    class _Fixed(datetime.datetime):
        _now = datetime.datetime(2026, 7, 15, 10, 0)

        @classmethod
        def now(cls, tz=None):
            return cls._now

    @staticmethod
    def _now():
        return AutomationEdgeBase._Fixed._now

    def _diary(self, date: str, summary: str = "暂无今日总结。"):
        path = settings.DIARY_DIR / f"{date}.md"
        path.write_text(
            f"# {date}\n\n<summary>\n{summary}\n</summary>\n\n---\n\n**09:00:** 内容",
            encoding="utf-8",
        )
        return path

    def _record(self, state: dict, task: str) -> dict:
        return state.setdefault("tasks", {}).setdefault(task, {})


class ModelReadyTests(AutomationEdgeBase):
    def test_model_ready_when_get_model_raises(self):
        with patch.object(automation.settings.ModelConfig, "get_model",
                          side_effect=RuntimeError("no models")):
            ready, reason = automation._model_ready()
        self.assertFalse(ready)
        self.assertIn("模型配置无效", reason)

    def test_model_ready_api_url_empty(self):
        with patch.object(automation.settings.ModelConfig, "get_model",
                          return_value={"name": "m", "api_url": "", "api_key": "x"}):
            ready, reason = automation._model_ready()
        self.assertFalse(ready)
        self.assertIn("api_url 为空", reason)

    def test_model_ready_api_key_empty(self):
        with patch.object(automation.settings.ModelConfig, "get_model",
                          return_value={"name": "m", "api_url": "https://x", "api_key": ""}):
            ready, reason = automation._model_ready()
        self.assertFalse(ready)
        self.assertIn("api_key 为空", reason)

    def test_model_ready_ok(self):
        with patch.object(automation.settings.ModelConfig, "get_model",
                          return_value={"name": "m", "api_url": "https://x", "api_key": "sk"}):
            ready, reason = automation._model_ready()
        self.assertTrue(ready)
        self.assertEqual("", reason)


class RunGenerationDispatchTests(AutomationEdgeBase):
    def test_run_generation_dispatches_weekly_and_monthly(self):
        with patch.object(automation, "summarize_diary", return_value=("日总结", True)) as sd, \
             patch.object(automation, "generate_analysis_report",
                          return_value=("周报", True, Path("/tmp/w.md"))) as gap, \
             patch.object(automation.settings.ModelConfig, "get_model",
                          return_value={"name": "mock"}):
            msg, ok = automation._run_generation(
                "daily_summary", {"start": "2026-07-14", "end": "2026-07-14"}
            )
            self.assertTrue(ok)
            sd.assert_called_once_with("2026-07-14", {"name": "mock"})

            # 周报分发到 generate_analysis_report("weekly", start, model)
            gap.reset_mock()
            m2, ok2 = automation._run_generation(
                "weekly_report", {"start": "2026-07-06", "end": "2026-07-12"}
            )
            self.assertTrue(ok2)
            gap.assert_called_once_with(
                "weekly", datetime.date(2026, 7, 6), {"name": "mock"}
            )

            # 月报分发到 generate_analysis_report("monthly", start, model)
            gap.reset_mock()
            m3, ok3 = automation._run_generation(
                "monthly_report", {"start": "2026-06-01", "end": "2026-06-30"}
            )
            self.assertTrue(ok3)
            gap.assert_called_once_with(
                "monthly", datetime.date(2026, 6, 1), {"name": "mock"}
            )


class RetryAndDetectionDueTests(AutomationEdgeBase):
    def test_retry_due_invalid_next_retry_at_is_not_due(self):
        # 解析不出合法时间 → 视为暂不重试（防御性，避免无限重试循环）。
        record = {"status": "failed", "next_retry_at": "not-a-date"}
        self.assertFalse(automation._retry_due(record, self._now()))

    def test_retry_due_pending_is_immediately_due(self):
        record = {"status": "pending", "next_retry_at": ""}
        self.assertTrue(automation._retry_due(record, self._now()))

    def test_detection_due_invalid_last_detection_returns_true(self):
        # 解析不出上次检测时间 → 立即重新检测（保守：宁可多扫）。
        state = {"last_detection_at": "garbage"}
        self.assertTrue(automation._detection_due(state, self._now()))


class BusyDeferralTests(AutomationEdgeBase):
    def test_report_lock_busy_keeps_pending_without_attempts(self):
        """报告锁被占用（如手动 report）：保留待生成（pending），不累计次数。"""
        self._diary("2026-07-14")
        self._diary("2026-07-08")  # 周报有内容
        from server.ai.analysis.orchestrator import REPORT_BUSY_MESSAGE
        with patch.object(automation, "_run_generation",
                          return_value=(REPORT_BUSY_MESSAGE, False)):
            automation.run_due_automatic_tasks()
        record = self._record(automation._load_automation_state(), "daily_summary")
        self.assertEqual("pending", record["status"])
        self.assertEqual(0, record["attempts"])
        self.assertEqual(REPORT_BUSY_MESSAGE, record["error"])


class ProcessDueSkipTests(AutomationEdgeBase):
    def test_blocked_task_does_not_regenerate(self):
        state = {"tasks": {"weekly_report": {
            "status": "blocked", "attempts": 3,
            "target_key": "2026-07-06|2026-07-12",
        }}}
        automation._save_automation_state(state)
        self._diary("2026-07-08")  # 周期有内容
        with patch.object(automation, "_model_ready", return_value=(True, "")), \
             patch.object(automation, "_run_generation",
                          side_effect=AssertionError("blocked 不应再生成")):
            automation._process_due(automation._load_automation_state(), self._now(),
                                    settings.CONFIG["automation"])
        rec = self._record(automation._load_automation_state(), "weekly_report")
        self.assertEqual("blocked", rec["status"])

    def test_fresh_running_task_is_not_restarted(self):
        """「正在生成」且未超时 → 等待，不重新调度。"""
        state = {"tasks": {"daily_summary": {
            "status": "running",
            "started_at": automation._now_text(self._now()),
            "target_key": "2026-07-14|2026-07-14",
        }}}
        automation._save_automation_state(state)
        self._diary("2026-07-14")
        with patch.object(automation, "_run_generation",
                          side_effect=AssertionError("非陈旧 running 不应重新生成")):
            automation._process_due(automation._load_automation_state(), self._now(),
                                    settings.CONFIG["automation"])


class AutomationConfigGuardTests(AutomationEdgeBase):
    def test_run_due_returns_when_automation_not_a_dict(self):
        settings.CONFIG["automation"] = "not-a-dict"
        # 不应抛异常（内部 logger.error 后返回）
        with patch("server.ai.analysis.automation.logger.error") as le:
            automation.run_due_automatic_tasks()
        le.assert_called()
        self.assertFalse((settings.ANALYSIS_DIR / ".automation-state.json").exists())

    def test_scan_pops_disabled_task_state(self):
        automation._save_automation_state({"tasks": {"weekly_report": {"status": "failed"}}})
        automation_configured = {"enabled": True, "daily_summary": True,
                                 "weekly_report": False, "monthly_report": True}
        state = automation._load_automation_state()
        automation._scan_missing(state, self._now(), automation_configured)
        self.assertNotIn("weekly_report", state.get("tasks", {}))


class ManualRetryGuardTests(AutomationEdgeBase):
    def test_retry_when_automation_disabled(self):
        settings.CONFIG["automation"] = {"enabled": False}
        ok, message = automation.retry_failed_automatic_tasks()
        self.assertFalse(ok)
        self.assertIn("停用", message)

    def test_retry_when_lock_busy(self):
        # 手动 retry 获取不到锁 → 安全返回
        with patch.object(automation, "_automation_lock", return_value=None):
            ok, message = automation.retry_failed_automatic_tasks()
        self.assertFalse(ok)
        self.assertIn("正在运行", message)

    def test_retry_internal_error_returns_false(self):
        class _FakeLock:
            def release(self):
                pass

        # 有失败任务，但重试过程中 _process_due 抛异常 → 应安全返回 (False, ...)
        automation._save_automation_state({"tasks": {"daily_summary": {
            "status": "failed", "error": "e", "attempts": 1,
            "next_retry_at": "", "target_key": "2026-07-14|2026-07-14",
        }}})
        with patch.object(automation, "_automation_lock", return_value=_FakeLock()), \
             patch.object(automation, "_process_due", side_effect=Exception("boom")):
            ok, message = automation.retry_failed_automatic_tasks()
        self.assertFalse(ok)
        self.assertIn("重试失败", message)


class StatusSnapshotRetryDueTests(AutomationEdgeBase):
    def test_failed_snapshot_marks_retry_due_when_past_next(self):
        state = {"tasks": {"daily_summary": {
            "status": "failed",
            "next_retry_at": automation._now_text(self._now() - datetime.timedelta(minutes=1)),
            "target_key": "2026-07-14|2026-07-14",
        }}}
        automation._save_automation_state(state)
        snapshot = automation.automation_status_snapshot()
        self.assertTrue(snapshot["tasks"]["daily_summary"].get("retry_due"))


if __name__ == "__main__":
    unittest.main()
