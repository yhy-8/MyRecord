"""orchestrator 的异常/边界路径测试（报告生成错误处理）。

覆盖现有 test_analysis 未触及的判定分支：
- Agent 输入超安全上限 / Agent 调用失败（agent_failed 日志与 usage 累计）
- 报告 JSON 顶层非对象 / summary 缺失 / references 非数组 / 无效引用剔除
- 每日总结格式校验的各类拒绝理由（非文本 / 空 / JSON / 代码围栏 / 正文内标题）
- 周期报告的公衡：非法 kind / 报告锁忙 / 无标准记录 / 空正文 / 内部异常
"""

import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.ai import settings
from server.ai.ai_client import AIResponse
from server.ai.analysis import orchestrator
from server.ai.agents import AgentPipelineError


def _tmp():
    root = Path(tempfile.mkdtemp(prefix="myrecord-orch-"))
    diary = root / "Records"
    analysis = root / "AnalysisReports"
    diary.mkdir(parents=True, exist_ok=True)
    analysis.mkdir(parents=True, exist_ok=True)
    return root, diary, analysis


class OrchestratorBase(unittest.TestCase):
    def setUp(self):
        self.root, self.diary, self.analysis = _tmp()
        self._patches = [
            patch.object(settings, "DIARY_DIR", self.diary),
            patch.object(settings, "ANALYSIS_DIR", self.analysis),
        ]
        for p in self._patches:
            p.start()
        self.model = {"name": "mock", "model_id": "mock-id",
                      "api_url": "https://example.test/v1", "api_key": "sk"}
        self.usage = orchestrator.UsageAccumulator()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def _diary(self, date, lines):
        body = "\n".join(lines)
        (self.diary / f"{date}.md").write_text(
            f"# {date}\n\n<summary>\n暂无今日总结。\n</summary>\n\n---\n"
            f"## 原始记录流\n\n{body}\n",
            encoding="utf-8",
        )


class SummaryValidationTests(OrchestratorBase):
    def test_non_text_summary_rejected(self):
        summary, errors = orchestrator._normalize_daily_summary(123)
        self.assertEqual("", summary)
        self.assertIn("总结不是文本", errors)

    def test_empty_summary_rejected(self):
        summary, errors = orchestrator._normalize_daily_summary("   ")
        self.assertIn("总结为空", errors)

    def test_json_summary_rejected(self):
        summary, errors = orchestrator._normalize_daily_summary('{"summary": "x"}')
        self.assertIn("总结输出了 JSON", errors)

    def test_code_fence_rejected(self):
        # 外圈 ``` 会被剥离；正文**内部**的代码围栏才是不允许的。
        summary, errors = orchestrator._normalize_daily_summary("正文\n```\n内\n```")
        self.assertIn("总结包含代码围栏", errors)

    def test_inner_summary_tag_rejected(self):
        summary, errors = orchestrator._normalize_daily_summary("文本 <summary>嵌</summary> 内容")
        self.assertIn("总结包含非外层 summary 标签", errors)

    def test_inner_heading_rejected(self):
        summary, errors = orchestrator._normalize_daily_summary("正文\n## 小标题\n内容")
        self.assertIn("总结包含正文内标题", errors)

    def test_outer_wrappers_are_stripped(self):
        summary, errors = orchestrator._normalize_daily_summary(
            "```markdown\n# 标题\n有效正文\n```"
        )
        self.assertEqual("有效正文", summary)
        self.assertEqual([], errors)

    def test_summarize_missing_diary_returns_not_found(self):
        msg, ok = orchestrator.summarize_diary("2099-01-01", self.model)
        self.assertFalse(ok)
        self.assertIn("找不到", msg)


class ReportParseEdgeTests(OrchestratorBase):
    def test_json_top_level_not_object_raises(self):
        with self.assertRaises(AgentPipelineError):
            orchestrator._parse_report_response("[1,2,3]", [])

    def test_summary_missing_raises(self):
        with self.assertRaises(AgentPipelineError):
            orchestrator._parse_report_response('{"references": []}', [])

    def test_summary_not_text_raises(self):
        with self.assertRaises(AgentPipelineError):
            orchestrator._parse_report_response('{"summary": 5}', [])

    def test_references_not_list_becomes_empty(self):
        summary, refs = orchestrator._parse_report_response(
            '{"summary": "正文。[1]", "references": "oops"}', []
        )
        self.assertEqual([], refs)

    def test_invalid_and_missing_id_references_dropped(self):
        records = [{"date": "2026-07-14", "line": 10, "text": "A"}]
        raw = ('{"summary": "正文", "references": ['
               '{"id": 1, "source": "R-20260714-10"},'
               '{"id": 2, "source": "R-NOPE-1"},'          # 格式非法 → 剔除
               '{"id": 3, "source": "R-20260714-99"},'     # 行号越界 → 剔除
               '{"source": "R-20260714-1"}]}')             # 缺 id，但来源合法 → 保留(id=None)
        summary, refs = orchestrator._parse_report_response(raw, records)
        # 排序键：非 int id 视为 0，故 id=None 排在最前
        self.assertEqual(
            [{"id": None, "source": "R-20260714-1"},
             {"id": 1, "source": "R-20260714-10"}],
            refs,
        )

    def test_ref_id_non_int_sorted_to_front(self):
        records = [{"date": "2026-07-14", "line": 5, "text": "A"}]
        raw = ('{"summary": "正文", "references": ['
               '{"id": 2, "source": "R-20260714-2"},'
               '{"id": "x", "source": "R-20260714-1"}]}')
        summary, refs = orchestrator._parse_report_response(raw, records)
        self.assertEqual(2, len(refs))
        self.assertEqual("x", refs[0]["id"])  # 非 int id 排序靠前

    def test_source_table_empty(self):
        self.assertEqual("", orchestrator._source_table([]))

    def test_source_table_rows(self):
        table = orchestrator._source_table([{"id": 1, "source": "R-20260714-1"},
                                            {"id": 2, "source": "R-20260714-2-3"}])
        self.assertEqual(
            "## 来源\n[1] R-20260714-1  \n[2] R-20260714-2-3  ", table
        )


class ReportInputAndAgentEdgeTests(OrchestratorBase):
    def test_report_input_groups_by_date(self):
        records = [{"date": "2026-07-14", "line": 1, "text": "A"},
                   {"date": "2026-07-14", "line": 2, "text": "B"},
                   {"date": "2026-07-15", "line": 3, "text": "C"}]
        result = orchestrator._report_input(records)
        self.assertEqual("[20260714]\n1: A\n2: B\n\n[20260715]\n3: C", result)

    def test_call_report_agent_input_too_large(self):
        with self.assertRaises(AgentPipelineError):
            orchestrator._call_report_agent(
                "task", "x" * (orchestrator._MAX_AGENT_INPUT_CHARACTERS + 1),
                self.model, self.usage, "run-1",
            )

    def test_call_report_agent_agent_failure_is_rethrown(self):
        with patch.object(orchestrator, "invoke_agent",
                          side_effect=AgentPipelineError("调用失败")), \
             patch("server.ai.analysis.orchestrator.logger.warning") as warn:
            with self.assertRaises(AgentPipelineError):
                orchestrator._call_report_agent(
                    "task", "input", self.model, self.usage, "run-1"
                )
        warn.assert_called_once()
        self.assertEqual(0, self.usage.totals()["total_tokens"])  # 本次无遥测

    def test_duration_label_formats(self):
        self.assertEqual("0.0 秒", orchestrator._duration_label(0.02))
        self.assertEqual("5.0 秒", orchestrator._duration_label(5.0))
        self.assertEqual("1 分 30 秒", orchestrator._duration_label(90))
        self.assertEqual("2 小时 3 分 4 秒", orchestrator._duration_label(2 * 3600 + 3 * 60 + 4))


class GenerateReportGuardTests(OrchestratorBase):
    def test_invalid_kind_rejected(self):
        msg, ok, path = orchestrator.generate_analysis_report(
            "bogus", datetime.date(2026, 7, 14), self.model
        )
        self.assertFalse(ok)
        self.assertIsNone(path)
        self.assertIn("只支持 weekly", msg)

    def test_report_lock_busy_returns_busy_message(self):
        with patch.object(orchestrator.FileLock, "acquire", return_value=None):
            msg, ok, path = orchestrator.generate_analysis_report(
                "weekly", datetime.date(2026, 7, 14), self.model
            )
        self.assertFalse(ok)
        self.assertEqual(orchestrator.REPORT_BUSY_MESSAGE, msg)

    def test_period_without_standard_records_fails_cleanly(self):
        # 文件存在，但正文没有可识别的标准记录（无 **HH:MM:** 头行）
        self._diary("2026-07-14", ["# 只有标题，无标准记录行", "<!-- myrecord-time:1 -->"])
        with patch.object(orchestrator, "call_ai"):
            msg, ok, path = orchestrator.generate_analysis_report(
                "weekly", datetime.date(2026, 7, 14), self.model
            )
        self.assertFalse(ok)
        self.assertIn("没有可识别的标准记录", msg)

    def test_empty_raw_body_fails_as_empty_report(self):
        # Agent 给出空正文 → 报告正文为空
        self._diary("2026-07-14", ["**09:00:** 一条标准记录"])
        with patch.object(orchestrator, "call_ai",
                          return_value=AIResponse("", True)):
            msg, ok, path = orchestrator.generate_analysis_report(
                "weekly", datetime.date(2026, 7, 14), self.model
            )
        self.assertFalse(ok)
        self.assertIn("报告正文为空", msg)

    def test_empty_json_summary_raises_analysis_failed(self):
        # 模型返回 summary 为空的 JSON → 解析抛错，最终归为「分析失败」
        self._diary("2026-07-14", ["**09:00:** 一条标准记录"])
        with patch.object(orchestrator, "call_ai",
                          return_value=AIResponse('{"summary": "", "references": []}', True)):
            msg, ok, path = orchestrator.generate_analysis_report(
                "weekly", datetime.date(2026, 7, 14), self.model
            )
        self.assertFalse(ok)
        self.assertIn("分析失败", msg)

    def test_internal_exception_becomes_analysis_failed(self):
        self._diary("2026-07-14", ["**09:00:** 一条标准记录"])
        with patch.object(orchestrator, "call_ai",
                          side_effect=Exception("boom")):
            msg, ok, path = orchestrator.generate_analysis_report(
                "weekly", datetime.date(2026, 7, 14), self.model
            )
        self.assertFalse(ok)
        self.assertIn("分析失败", msg)
        self.assertIn("boom", msg)


if __name__ == "__main__":
    unittest.main()
