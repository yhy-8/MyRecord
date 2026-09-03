"""服务端 CLI（server/main.py）：token / import / render 子命令测试。

通过替换 config.load 指向独立临时数据目录，避免写入仓库真实 data/。
"""

import argparse
import datetime
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import main as server_main
from server.hub.store import Store


def _data_dir_config(data_dir: Path) -> dict:
    """与真实 server.config.load 一致：包含由 data_dir 推导出的缺省 TLS 路径。"""
    return {
        "server": {
            "data_dir": str(data_dir),
            "tls": {
                "certfile": str(data_dir / "tls" / "server.crt"),
                "keyfile": str(data_dir / "tls" / "server.key"),
            },
        }
    }


class ServerMainTokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_dir = self.root / "data"
        self._orig_load = server_main.config.load
        server_main.config.load = lambda: _data_dir_config(self.data_dir)

    def tearDown(self):
        server_main.config.load = self._orig_load
        self.tmp.cleanup()

    def _capture(self, argv, stdin_sides=None):
        out = io.StringIO()
        patchers = [patch("sys.stdout", out)]
        if stdin_sides is not None:
            patchers.append(patch("builtins.input", side_effect=stdin_sides))
        for p in patchers:
            p.start()
        try:
            rc = server_main.main(argv)
        finally:
            for p in patchers:
                p.stop()
        return rc, out.getvalue()

    @staticmethod
    def _token_from(text):
        for line in text.splitlines():
            if line.startswith("token: "):
                return line[len("token: "):].strip()
        return None

    def test_token_create_and_list(self):
        # 单一链接凭证：create 无需 --device，签发唯一 token
        rc, text = self._capture(["token", "create"])
        self.assertEqual(0, rc)
        self.assertIn("链接凭证已签发", text)
        self.assertIn("token: ", text)
        token = self._token_from(text)
        self.assertIsNotNone(token)

        # 凭证不绑定设备名：任意自报名字都能通过校验（单一凭证模型）
        store = Store(self.data_dir / "state.json")
        self.assertTrue(store.verify_device("whatever", token))

        rc, listing = self._capture(["token", "list"])
        self.assertEqual(0, rc)
        self.assertIn("已配置", listing)
        self.assertIn("生成于", listing)  # list 附带凭证生成时间

    def test_token_list_unconfigured(self):
        rc, text = self._capture(["token", "list"])
        self.assertEqual(0, rc)
        self.assertIn("未配置", text)

    def test_token_create_needs_no_device_arg(self):
        rc, text = self._capture(["token", "create"])
        self.assertEqual(0, rc)
        self.assertIn("token: ", text)

    def test_token_create_overwrite_requires_confirmation(self):
        rc, _ = self._capture(["token", "create"])
        self.assertEqual(0, rc)

        # 已存在凭证：不输入 yes → 取消，不覆盖
        err = io.StringIO()
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", err), patch(
            "builtins.input", return_value="no"
        ):
            rc = server_main.main(["token", "create"])
        self.assertEqual(1, rc)
        self.assertIn("已取消", err.getvalue())

    def test_token_create_overwrite_confirms_and_replaces(self):
        rc, first_text = self._capture(["token", "create"])
        self.assertEqual(0, rc)
        token1 = self._token_from(first_text)
        self.assertIsNotNone(token1)

        # 输入 yes 确认覆盖 → 签发新 token，旧 token 立即失效
        rc, second_text = self._capture(["token", "create"], stdin_sides=["yes"])
        self.assertEqual(0, rc)
        token2 = self._token_from(second_text)

        store = Store(self.data_dir / "state.json")
        self.assertNotEqual(token1, token2)
        self.assertFalse(store.verify_device("sync", token1))
        self.assertTrue(store.verify_device("sync", token2))


class ServerMainDeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_dir = self.root / "data"
        self._orig_load = server_main.config.load
        server_main.config.load = lambda: _data_dir_config(self.data_dir)
        # 预置证书文件，让 deploy 跳过自动生成
        tls = self.data_dir / "tls"
        tls.mkdir(parents=True, exist_ok=True)
        (tls / "server.crt").write_text("fakepem", encoding="utf-8")
        (tls / "server.key").write_text("fakepem", encoding="utf-8")

    def tearDown(self):
        server_main.config.load = self._orig_load
        self.tmp.cleanup()

    def test_render_systemd_includes_interpreter_and_workdir_but_no_install(self):
        text = server_main._render_systemd("/usr/bin/python3", Path("/srv/myrecord"))
        self.assertIn("ExecStart=/usr/bin/python3 -m server.main run", text)
        self.assertIn("WorkingDirectory=/srv/myrecord", text)
        # 默认不启用开机自启：单元不带 [Install] 段
        self.assertNotIn("WantedBy=", text)

    def test_deploy_requires_root(self):
        err = io.StringIO()
        with patch("server.main.os.geteuid", return_value=1000, create=True), patch(
            "sys.stdout", io.StringIO()
        ), patch("sys.stderr", err), patch("server.main.subprocess.run") as run:
            rc = server_main.main(["deploy"])
        self.assertEqual(2, rc)
        self.assertIn("root", err.getvalue())
        run.assert_not_called()

    def test_render_backup_unit_uses_backup_script_and_workdir(self):
        text = server_main._render_backup_unit(Path("/srv/myrecord"))
        self.assertIn("ExecStart=/bin/bash /srv/myrecord/server/deploy/backup.sh", text)
        self.assertIn("WorkingDirectory=/srv/myrecord", text)

    def test_deploy_installs_server_and_backup_and_enables_timer(self):
        server_unit = self.root / "systemd" / "myrecord-server.service"
        backup_unit = self.root / "systemd" / "myrecord-backup.service"
        timer_unit = self.root / "systemd" / "myrecord-backup.timer"
        with patch("server.main.os.geteuid", return_value=0, create=True), patch(
            "server.main._SYSTEMD_UNIT_PATH", server_unit
        ), patch("server.main._BACKUP_SERVICE_PATH", backup_unit), patch(
            "server.main._BACKUP_TIMER_PATH", timer_unit
        ), patch("sys.stdout", io.StringIO()), patch("server.main.subprocess.run") as run:
            rc = server_main.main(["deploy"])
        self.assertEqual(0, rc)

        import sys as _sys
        self.assertIn(
            f"ExecStart={_sys.executable} -m server.main run",
            server_unit.read_text(encoding="utf-8"),
        )
        backup_text = backup_unit.read_text(encoding="utf-8")
        self.assertIn("backup.sh", backup_text)
        self.assertIn("ExecStart=/bin/bash", backup_text)
        self.assertIn("WorkingDirectory=", backup_text)
        self.assertIn("OnCalendar=weekly", timer_unit.read_text(encoding="utf-8"))

        calls = [c.args[0] for c in run.call_args_list]
        self.assertEqual(
            calls,
            [
                ["systemctl", "daemon-reload"],
                ["systemctl", "start", "myrecord-server"],
                ["systemctl", "enable", "--now", "myrecord-backup.timer"],
            ],
        )


class ServerMainRenderImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_dir = self.root / "data"
        self._orig_load = server_main.config.load
        server_main.config.load = lambda: _data_dir_config(self.data_dir)

    def tearDown(self):
        server_main.config.load = self._orig_load
        self.tmp.cleanup()

    def test_render_writes_records_from_store(self):
        store = Store(self.data_dir / "state.json")
        store.append_entries(
            "legacy",
            [{
                "entry_id": "a-1",
                "date": "2024-01-01",
                "ts": 1704067200,
                "tag": "",
                "text": "hello",
            }],
        )
        with patch("sys.stdout", io.StringIO()):
            rc = server_main.main(["render"])
        self.assertEqual(0, rc)
        rendered = (self.data_dir / "Records" / "2024-01-01.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("hello", rendered)
        self.assertIn("a-1", rendered)

    def test_import_legacy_records_appends_and_renders(self):
        legacy = self.root / "legacy-records"
        legacy.mkdir()
        (legacy / "2024-01-01.md").write_text(
            "# 2024-01-01\n\n<summary>\n暂无今日总结。\n</summary>\n\n---\n"
            "## 原始记录流\n\n<!-- agentrecord-record -->\n**08:00:** 旧记录\n",
            encoding="utf-8",
        )
        # 非日期文件应被跳过
        (legacy / "notes.md").write_text("不是日记", encoding="utf-8")

        with patch("sys.stdout", io.StringIO()):
            rc = server_main.main(["import", "--records", str(legacy)])
        self.assertEqual(0, rc)

        store = Store(self.data_dir / "state.json")
        self.assertEqual(1, len(store.data["entries"]))
        entry = next(iter(store.data["entries"].values()))
        self.assertEqual("legacy", entry["device_id"])
        self.assertEqual("旧记录", entry["text"])
        rendered = (self.data_dir / "Records" / "2024-01-01.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("旧记录", rendered)

    def test_import_missing_directory_returns_error(self):
        with patch("sys.stdout", io.StringIO()):
            rc = server_main.main(
                ["import", "--records", str(self.root / "nope")]
            )
        self.assertEqual(2, rc)


class ServerMainReportTests(unittest.TestCase):
    """手动生成周报/月报：与自动任务同一流程、同一路径（补足原先缺手动入口）。"""

    def test_report_rejects_invalid_date(self):
        with patch("sys.stderr", io.StringIO()):
            rc = server_main.main(
                ["report", "--kind", "weekly", "--date", "not-a-date"]
            )
        self.assertEqual(2, rc)

    def test_report_generates_weekly_overwrites_same_path(self):
        with patch("server.ai.settings.ModelConfig.get_model", return_value={"name": "mock"}), \
             patch(
                 "server.ai.analysis.generate_analysis_report",
                 return_value=("生成成功", True, Path("/tmp/r.md")),
             ) as gen, \
             patch("builtins.print"):
            rc = server_main._command_report(
                argparse.Namespace(kind="weekly", date="2026-07-14")
            )
        self.assertEqual(0, rc)
        gen.assert_called_once_with(
            "weekly", datetime.date(2026, 7, 14), {"name": "mock"}
        )

    def test_report_failures_return_nonzero(self):
        with patch("server.ai.settings.ModelConfig.get_model", return_value={"name": "mock"}), \
             patch(
                 "server.ai.analysis.generate_analysis_report",
                 return_value=("分析失败", False, None),
             ), \
             patch("builtins.print"):
            rc = server_main._command_report(
                argparse.Namespace(kind="monthly", date="2026-07-14")
            )
        self.assertEqual(1, rc)


if __name__ == "__main__":
    unittest.main()