"""server/main.py 的 `run` 命令（_command_run）路径测试。

补齐 startup 主路径覆盖（覆盖率黑洞 25-158）：
- 证书缺失 → 拒绝启动（返回码 2，禁止明文）
- 证书存在 → 启动并把这些回调接入 hub_server.serve：
    list_reports / read_report / automation_status / admin_retry / admin_set_model /
    status_ai（以及后台 run_ai_cycle 的兜底渲染）
- 验证这些回调在真实数据目录下的行为（越界防护、目录枚举、报告读取等）。
"""

import argparse
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import main as server_main
from server.ai import settings as ai_settings  # 确保 settings 在 patch config 前已加载（真实 Path）
from server.hub.store import Store, today_utc8


def _data_dir_config(data_dir: Path) -> dict:
    return {
        "server": {
            "host": "0.0.0.0",
            "port": 8765,
            "data_dir": str(data_dir),
            "tls": {
                "certfile": str(data_dir / "tls" / "server.crt"),
                "keyfile": str(data_dir / "tls" / "server.key"),
            },
        }
    }


def _make_store(data_dir: Path) -> Store:
    store = Store(data_dir / "state.json")
    return store


class RunCommandTLSGateTest(unittest.TestCase):
    """强制 TLS：证书/密钥缺失时 `run` 拒绝启动（禁止明文）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data_dir = self.root / "data"
        self._orig_load = server_main.config.load
        server_main.config.load = lambda: _data_dir_config(self.data_dir)
        # 用 addCleanup 而非 tearDown：即使 setUp 中途失败（如证书生成抛错），
        # 也能恢复 monkeypatch，避免污染后续 test_* 模块（尤其 test_server_config 的 config.load）。
        self.addCleanup(self._restore_config_load)

    def _restore_config_load(self):
        server_main.config.load = self._orig_load

    def test_run_rejects_start_without_tls_certs(self):
        err = io.StringIO()
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", err), \
             patch("server.ai.analysis._purge_empty_placeholder_days"), \
             patch("server.ai.logging_config.configure_logging"):
            rc = server_main._command_run(argparse.Namespace())
        self.assertEqual(2, rc, "未配置 TLS 应拒绝启动")
        self.assertIn("TLS 证书", err.getvalue())


class RunCommandCallbacksTest(unittest.TestCase):
    """证书存在时启动：serve 接到的回调在真实数据目录下行为正确。"""

    def setUp(self):
        # `run` 的证书存在分支需要真实自签证书（_generate_cert 依赖 cryptography）；
        # 该依赖缺失时不该让整组用例硬出错，按仓库既有惯例（test_terminal_input / test_real_ai）
        # 优雅跳过，并避免 setUp 中途抛错导致 monkeypatch 泄漏。
        try:
            from cryptography import x509  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("缺少 cryptography：`pip install cryptography`")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data_dir = self.root / "data"
        self._orig_load = server_main.config.load
        server_main.config.load = lambda: _data_dir_config(self.data_dir)
        self.addCleanup(self._restore_config_load)
        server_main._generate_cert(self.data_dir)  # 真实自签证书
        self.captured = {}
        self._orig_serve = server_main.hub_server.serve
        self.addCleanup(self._restore_serve)
        import server.ai.analysis as analysis
        self._orig_run_due = analysis.run_due_automatic_tasks
        self.addCleanup(self._restore_run_due)

    def _restore_config_load(self):
        server_main.config.load = self._orig_load

    def _restore_serve(self):
        server_main.hub_server.serve = self._orig_serve

    def _restore_run_due(self):
        import server.ai.analysis as analysis
        analysis.run_due_automatic_tasks = self._orig_run_due

    def _fake_serve(self, store, host, port, **kwargs):
        self.captured = {"store": store, "host": host, "port": port, **kwargs}
        class FakeHttpd:
            def serve_forever(self, *a, **k):
                raise KeyboardInterrupt()  # 启动后立即退出，避免阻塞
        return FakeHttpd()

    def test_run_starts_serving_and_wires_callbacks(self):
        # 准备一处报告 + 一处历史日 Records，供回调读取
        store = _make_store(self.data_dir)
        today = today_utc8()
        store.append_entries(
            "cb", [{"entry_id": "a-1", "date": today, "ts": 1788652800000,
                    "tag": "", "text": "hello"}]
        )
        reports = self.data_dir / "AnalysisReports" / "Weekly"
        reports.mkdir(parents=True, exist_ok=True)
        (reports / "2026-07-06_to_2026-07-12.md").write_text("# 周报内容", encoding="utf-8")
        records = self.data_dir / "Records"
        records.mkdir(parents=True, exist_ok=True)
        (records / "2000-01-01.md").write_text("# 2000-01-01\n\n旧内容", encoding="utf-8")
        (self.data_dir / "AnalysisReports" / ".automation-state.json").write_text(
            '{"tasks": {"weekly_report": {"status": "ok"}}}', encoding="utf-8"
        )

        import server.ai.analysis as analysis
        import server.hub.backup as backup
        # 不依赖仓库是否配置了真实 config.yaml：提供确定性模型配置，
        # 使 status_ai / admin_set_model 可独立验证。
        fake_config = {
            "current_model": "deepseek-v4-flash",
            "models": [
                {"name": "deepseek-v4-flash", "model_id": "deepseek-v4-flash",
                 "api_url": "https://x", "api_key": "k"},
                {"name": "deepseek-v4-pro", "model_id": "deepseek-v4-pro",
                 "api_url": "https://x", "api_key": "k"},
            ],
        }
        # 阻止 _command_run 真实启动后台 daemon 线程（避免它跨出 with 后漏执行备份/循环）
        with patch("server.main.hub_server.serve", self._fake_serve), \
             patch.object(analysis, "run_due_automatic_tasks"), \
             patch.object(analysis, "retry_failed_automatic_tasks",
                          return_value=(True, "全部重试成功")), \
             patch.object(backup, "run_backup_if_due"), \
             patch("server.main.print"), \
             patch("threading.Thread"), \
             patch("server.ai.logging_config.configure_logging"), \
             patch.dict(ai_settings.CONFIG, fake_config), \
             patch.object(
                 ai_settings.ModelConfig, "select",
                 side_effect=lambda name: (
                     (_ for _ in ()).throw(KeyError(name)) if name == "ghost-model"
                     else {"name": name}
                 ),
             ):
            rc = server_main._command_run(argparse.Namespace())

        self.assertEqual(0, rc)
        self.assertEqual("0.0.0.0", self.captured["host"])
        # 回调均已接入
        for name in ("list_reports", "read_report", "automation_status",
                     "admin_retry", "admin_set_model", "status_ai"):
            self.assertIn(name, self.captured)

        # --- list_reports：仅枚举 AnalysisReports 内 .md，剔除越界 ---
        lr = self.captured["list_reports"]
        self.assertEqual(["Weekly/2026-07-06_to_2026-07-12.md"], lr(""))
        self.assertEqual(["Weekly/2026-07-06_to_2026-07-12.md"], lr("Weekly"))
        self.assertEqual([], lr("weekly"))  # 大小写敏感：kind 必须与目录名一致
        self.assertEqual([], lr("../../../../etc"))  # 越界/非目录被剔除

        # --- read_report：正常读取；越界 / 缺失返回 None ---
        rr = self.captured["read_report"]
        self.assertEqual("# 周报内容", rr("Weekly/2026-07-06_to_2026-07-12.md"))
        self.assertIsNone(rr("../outside.md"))
        self.assertIsNone(rr("NoSuch.md"))

        # --- automation_status：读 .automation-state.json ---
        st = self.captured["automation_status"]()
        self.assertEqual("ok", st["tasks"]["weekly_report"]["status"])

        # --- admin_retry / admin_set_model / status_ai：经由真实 AI 模块 ---
        # 重新注入确定性模型配置（out of the with 后 CONFIG 已还原），验证这些回调。
        with patch.dict(ai_settings.CONFIG, fake_config):
            # status_ai 返回当前模型清单
            ai = self.captured["status_ai"]()
            self.assertIn("current_model", ai)
            self.assertIn("models", ai)

            # admin_set_model 对不存在模型返回 (False, ...)，不抛异常（get_model 抛错，不落盘）
            ok, msg = self.captured["admin_set_model"]("ghost-model")
            self.assertFalse(ok)
            self.assertIn("失败", msg)

            # admin_set_model 切到已配置模型 → success（select 已被 patch，不落盘 config.yaml）
            ok_name = ai_settings.ModelConfig.models()[0]["name"]
            with patch.object(
                ai_settings.ModelConfig, "select",
                side_effect=lambda name: {"name": name},
            ):
                ok, msg = self.captured["admin_set_model"](ok_name)
            self.assertTrue(ok)

            # admin_retry 走真实 retry 路径（已 patch，无失败任务 → 成功）
            with patch.object(
                analysis, "retry_failed_automatic_tasks",
                return_value=(True, "全部重试成功"),
            ):
                ok, msg = self.captured["admin_retry"]()
            self.assertTrue(ok)
            self.assertIn("重试成功", msg)


class MainDispatchTest(unittest.TestCase):
    """main() 命令分发：默认 run。"""

    def test_main_no_command_defaults_to_run(self):
        with patch("server.main._command_run", return_value=7) as run:
            rc = server_main.main([])
        self.assertEqual(7, rc)
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
