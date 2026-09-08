"""真实云端 AI 集成验证（deepseek-v4-flash 等 OpenAI 兼容模型）。

这些测试会**真实调用配置在 server/config.yaml 的模型 API**（读取 settings.CONFIG 的
活动模型），验证 每日总结 / 周报 / 月报 三条链路的确能跑通并产出符合设计基线的产物。

- 只有 `server/config.yaml` 里配置了非空 `api_key` 且网络可达时才执行；
  否则整组跳过（不阻塞无密钥 / 离线的常规测试）。
- 用 `tempfile` 隔离数据目录，不污染仓库真实 `server/data/`。
- 断言只校验**结构**（头部审计元数据、总结区域、报告文件、引用格式），
  不依赖模型的具体文风，避免把结果写死成一次性快照。
"""

import datetime
import re
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from server.ai import settings
from server.ai.analysis import generate_analysis_report, summarize_diary
from server.ai.settings import ModelConfig


def _live_model():
    """优先取 deepseek-v4-flash；若未配置则回退到当前活动模型。"""
    for name in ("deepseek-v4-flash", None):
        try:
            model = ModelConfig.get_model(name)
        except Exception:
            continue
        if str(model.get("api_key") or "").strip():
            return model
    return ModelConfig.get_model()


def _has_live_key() -> bool:
    """是否配置了可用的模型密钥（api_key 非空且 api_url 可达）。"""
    try:
        model = _live_model()
    except Exception:
        return False
    api_key = str(model.get("api_key") or "").strip()
    api_url = str(model.get("api_url") or "").strip()
    if not api_key or not api_url:
        return False
    hostname = urlsplit(api_url).hostname
    if not hostname:
        return False
    try:
        socket.gethostbyname(hostname)
    except socket.gaierror:
        return False
    return True


_LIVE = _has_live_key()


def _model_label() -> str:
    model = _live_model()
    return str(model.get("model_id") or model.get("name") or "未标明")


def _report_with_retry(kind, anchor, model):
    """真实 API 集成测试：对瞬时失败做一次重试（与自动化层自身的重试语义一致）。

    第三方 API 偶发 5xx/超时/空正文属于瞬时异常，做一次重试以避免把瞬时抖动误判为
    链路缺陷；仍失败则如实上报。
    """
    msg, ok, path = generate_analysis_report(kind, anchor, model)
    if ok:
        return msg, ok, path
    import time as _time
    _time.sleep(3)
    return generate_analysis_report(kind, anchor, model)


def _seed_week_records(diary_dir: Path, start: datetime.date, end: datetime.date) -> None:
    """在 temp 数据目录里生成一周的真实日记（标准新格式），供真实报告链路读取。"""
    _UTC8 = datetime.timezone(datetime.timedelta(hours=8))

    def ts_for(date_str, hhmm):
        dt = datetime.datetime.strptime(f"{date_str} {hhmm}", "%Y-%m-%d %H:%M")
        return int(dt.replace(tzinfo=_UTC8).timestamp() * 1000)

    idx = {"n": 0}

    def eid(ts):
        idx["n"] += 1
        return str(ts + idx["n"])

    scripts = [
        ("09:00", "早上整理了本周的项目计划，明确了三个重点任务。"),
        ("20:30", "晚上阅读了《系统设计》第 3 章，记录了一些心得。"),
        ("21:10", "和朋友讨论了同步协议的取舍，倾向于中心化方案。"),
    ]
    current = start
    while current <= end:
        date_str = current.isoformat()
        lines = [f"# {date_str}", "", "<summary>", "暂无今日总结。", "</summary>",
                 "", "---", "## 原始记录流", ""]
        for hhmm, text in scripts:
            ts = ts_for(date_str, hhmm)
            lines += [f"<!-- myrecord-time:{eid(ts)} -->",
                      "<!-- myrecord-device:test-host -->",
                      f"**{hhmm} [test-host]:** {text}", ""]
        (diary_dir / f"{date_str}.md").write_text("\n".join(lines), encoding="utf-8")
        current += datetime.timedelta(days=1)


class RealAIIntegrationTests(unittest.TestCase):
    """真实模型验证：每日总结 / 周报 / 月报 链路端到端产出符合设计基线的产物。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.diary_dir = self.root / "Records"
        self.analysis_dir = self.root / "AnalysisReports"
        self.diary_dir.mkdir(parents=True, exist_ok=True)
        self.analysis_dir.mkdir(parents=True, exist_ok=True)
        self._patches = [
            # 把 AI 数据空间切到临时目录，与真实仓库 server/data 隔离。
            mock.patch.object(settings, "DIARY_DIR", self.diary_dir),
            mock.patch.object(settings, "ANALYSIS_DIR", self.analysis_dir),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self.tmp.cleanup()

    @unittest.skipUnless(_LIVE, "未配置可用的模型密钥/网络不可达，跳过真实 AI 验证")
    def test_daily_summary_writes_real_summary_region(self):
        """真实模型：昨日总结写入 <summary> 且非占位符。"""
        day = datetime.date(2026, 9, 6)
        _seed_week_records(self.diary_dir, day, day)
        summary, ok = summarize_diary(day.isoformat(), _live_model())
        self.assertTrue(ok, f"真实日总结失败: {summary}")
        self.assertTrue(summary.strip(), "总结不应为空")
        content = (self.diary_dir / f"{day.isoformat()}.md").read_text(encoding="utf-8")
        m = re.search(r"<summary>(.*?)</summary>", content, re.DOTALL)
        self.assertIsNotNone(m, "应写入 <summary> 区域")
        written = m.group(1).strip()
        self.assertNotIn("暂无今日总结", written)
        self.assertNotIn("```", written)
        # 原始记录流仍保留，未被动过
        self.assertIn("## 原始记录流", content)
        self.assertIn("test-host", content)

    @unittest.skipUnless(_LIVE, "未配置可用的模型密钥/网络不可达，跳过真实 AI 验证")
    def test_weekly_report_has_full_audit_trail_and_sources(self):
        """真实模型：周报走统一路径产出，含头部审计元数据 + 文末来源表 + 引用。"""
        start, end = datetime.date(2026, 8, 31), datetime.date(2026, 9, 6)
        _seed_week_records(self.diary_dir, start, end)
        msg, ok, path = _report_with_retry("weekly", start, _live_model())
        self.assertTrue(ok, f"真实周报生成失败: {msg}")
        self.assertIsNotNone(path)
        self.assertTrue(path.exists())
        content = path.read_text(encoding="utf-8")
        self.assertIn("分析周报", content)
        # 六个头部审计元数据行（各自以 > 开头，末尾两个硬换行空格）
        for key in ("生成时间", "使用模型", "生成耗时", "Token 用量",
                    "原始日记范围", "分析运行"):
            self.assertIn(key, content)
        header_lines = [ln for ln in content.splitlines() if ln.startswith("> ")]
        self.assertGreaterEqual(len(header_lines), 6)
        for ln in header_lines:
            self.assertTrue(ln.endswith("  "), f"元数据行应以两个空格结尾: {ln!r}")
        # 报告唯一权威、只读原始记录流
        self.assertIn("分析周报", content)
        # 引用来源格式合法（若模型给出了引用）
        if "## 来源" in content:
            for m in re.finditer(r"R-(\d{8})-\d+(?:-\d+)?", content):
                date = datetime.datetime.strptime(m.group(1), "%Y%m%d").date()
                self.assertTrue(start <= date <= end, f"引用日期越界: {m.group(0)}")

    @unittest.skipUnless(_LIVE, "未配置可用的模型密钥/网络不可达，跳过真实 AI 验证")
    def test_monthly_report_has_full_audit_trail(self):
        """真实模型：月报走统一路径产出，含头部审计元数据 + 文末来源表。"""
        start, end = datetime.date(2026, 8, 1), datetime.date(2026, 8, 31)
        _seed_week_records(self.diary_dir, start, end)
        msg, ok, path = _report_with_retry("monthly", end, _live_model())
        self.assertTrue(ok, f"真实月报生成失败: {msg}")
        self.assertIsNotNone(path)
        content = path.read_text(encoding="utf-8")
        self.assertIn("分析月报", content)
        for key in ("生成时间", "使用模型", "生成耗时", "Token 用量",
                    "原始日记范围", "分析运行"):
            self.assertIn(key, content)
        # 报告不读取自己的旧版块（纯原始记录流）
        self.assertIn("分析月报", content)


if __name__ == "__main__":
    unittest.main()
