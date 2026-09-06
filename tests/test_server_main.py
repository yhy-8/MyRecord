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
from server.hub.store import Store, today_utc8


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

    def test_render_systemd_uses_actual_package_name(self):
        # 服务端工程改名后，`-m <新包名>.main` 应由当前包名反推，不硬编码 server。
        with patch("server.main._package_name", return_value="backend"):
            text = server_main._render_systemd("/usr/bin/python3", Path("/srv/myrecord"))
        self.assertIn("ExecStart=/usr/bin/python3 -m backend.main run", text)

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
        backup_script = (server_main._deploy_dir() / "backup.sh").as_posix()
        self.assertIn(f"ExecStart=/bin/bash {backup_script}", text)
        self.assertIn("WorkingDirectory=/srv/myrecord", text)

    def test_deploy_installs_server_and_backup_and_starts_timer(self):
        # 一键部署：自动建 venv + 装依赖，服务用 venv 的 python；主服务与备份定时器都只 start、不 enable。
        server_unit = self.root / "systemd" / "myrecord-server.service"
        backup_unit = self.root / "systemd" / "myrecord-backup.service"
        timer_unit = self.root / "systemd" / "myrecord-backup.timer"
        fake_venv = Path("/srv/myrecord/server/.venv")
        import sys as _sys
        out = io.StringIO()
        with patch("server.main.os.geteuid", return_value=0, create=True), patch(
            "server.main._SYSTEMD_UNIT_PATH", server_unit
        ), patch("server.main._BACKUP_SERVICE_PATH", backup_unit), patch(
            "server.main._BACKUP_TIMER_PATH", timer_unit
        ), patch("server.main._venv_dir", return_value=fake_venv), patch(
            "server.main._running_in_venv", return_value=True
        ), patch(
            "sys.stdout", out
        ), patch("server.main.subprocess.run") as run:
            rc = server_main.main(["deploy"])
        self.assertEqual(0, rc)

        # 部署摘要：虚拟环境/自签证书/链接凭证/API 配置/服务部署都要说明到。
        summary = out.getvalue()
        self.assertIn("部署完成", summary)
        self.assertIn("虚拟环境", summary)
        self.assertIn("自签证书", summary)
        self.assertIn("链接凭证", summary)
        self.assertIn("API 配置", summary)
        self.assertIn("服务部署", summary)
        self.assertIn(str(fake_venv), summary)  # 明确服务切到虚拟环境

        self.assertIn(
            "ExecStart=/srv/myrecord/server/.venv/bin/python -m server.main run",
            server_unit.read_text(encoding="utf-8"),
        )
        backup_text = backup_unit.read_text(encoding="utf-8")
        self.assertIn("backup.sh", backup_text)
        self.assertIn("ExecStart=/bin/bash", backup_text)
        self.assertIn("WorkingDirectory=", backup_text)
        self.assertIn("OnCalendar=weekly", timer_unit.read_text(encoding="utf-8"))

        calls = [c.args[0] for c in run.call_args_list]
        venv_py = (fake_venv / "bin" / "python").as_posix()
        reqs = (Path(server_main.__file__).resolve().parent / "requirements.txt").as_posix()
        # 前两步：用当前解释器建 venv，再用 venv 的 pip 安装依赖。
        self.assertEqual(calls[0], [_sys.executable, "-m", "venv", fake_venv.as_posix()])
        self.assertEqual(calls[1], [venv_py, "-m", "pip", "install", "-r", reqs])
        # 最后三步：只 start、不 enable。
        self.assertEqual(
            calls[2:],
            [
                ["systemctl", "daemon-reload"],
                ["systemctl", "start", "myrecord-server"],
                ["systemctl", "start", "myrecord-backup.timer"],
            ],
        )
        # 主服务与备份定时器都只 start、不 enable 开机自启。
        for c in calls:
            self.assertNotIn("enable", c)

    def test_deploy_redeploy_stops_existing_then_overwrites_and_starts(self):
        """迭代升级/重装：同名服务单元已存在时，先 stop 旧服务，再覆盖新单元并重新 start。
        仍是 start、不 enable 开机自启；覆盖后写入新单元内容。"""
        server_unit = self.root / "systemd" / "myrecord-server.service"
        backup_unit = self.root / "systemd" / "myrecord-backup.service"
        timer_unit = self.root / "systemd" / "myrecord-backup.timer"
        server_unit.parent.mkdir(parents=True, exist_ok=True)
        # 模拟已部署过：旧单元与旧定时器文件已存在（旧进程正在跑旧代码）。
        server_unit.write_text("old-server", encoding="utf-8")
        timer_unit.write_text("old-timer", encoding="utf-8")
        fake_venv = Path("/srv/myrecord/server/.venv")
        import sys as _sys

        with patch("server.main.os.geteuid", return_value=0, create=True), patch(
            "server.main._SYSTEMD_UNIT_PATH", server_unit
        ), patch("server.main._BACKUP_SERVICE_PATH", backup_unit), patch(
            "server.main._BACKUP_TIMER_PATH", timer_unit
        ), patch("server.main._venv_dir", return_value=fake_venv), patch(
            "server.main._running_in_venv", return_value=True
        ), patch(
            "sys.stdout", io.StringIO()
        ), patch("server.main.subprocess.run") as run:
            rc = server_main.main(["deploy"])
        self.assertEqual(0, rc)

        calls = [c.args[0] for c in run.call_args_list]
        venv_py = (fake_venv / "bin" / "python").as_posix()
        reqs = (Path(server_main.__file__).resolve().parent / "requirements.txt").as_posix()
        self.assertEqual(
            calls,
            [
                [_sys.executable, "-m", "venv", fake_venv.as_posix()],
                [venv_py, "-m", "pip", "install", "-r", reqs],
                ["systemctl", "stop", "myrecord-server"],
                ["systemctl", "stop", "myrecord-backup.timer"],
                ["systemctl", "daemon-reload"],
                ["systemctl", "start", "myrecord-server"],
                ["systemctl", "start", "myrecord-backup.timer"],
            ],
        )
        # 仍不 enable 开机自启。
        for c in calls:
            self.assertNotIn("enable", c)
        # 旧单元被新内容覆盖。
        self.assertIn(
            "ExecStart=/srv/myrecord/server/.venv/bin/python -m server.main run",
            server_unit.read_text(encoding="utf-8"),
        )

    def test_deploy_bootstraps_into_venv_when_not_in_venv(self):
        """不在目标 venv 内运行时：先建 venv，再用 venv 的 python 重新执行 deploy。
        这样无需预先 pip install 到默认/系统 Python，依赖一律装进 server/.venv。"""
        fake_venv = Path("/srv/myrecord/server/.venv")
        import sys as _sys
        out = io.StringIO()
        with patch("server.main.os.geteuid", return_value=0, create=True), patch(
            "server.main._SYSTEMD_UNIT_PATH", self.root / "x.service"
        ), patch("server.main._BACKUP_SERVICE_PATH", self.root / "y.service"), patch(
            "server.main._BACKUP_TIMER_PATH", self.root / "z.timer"
        ), patch("server.main._venv_dir", return_value=fake_venv), patch(
            "server.main._running_in_venv", return_value=False
        ), patch(
            "sys.stdout", out
        ), patch("server.main.subprocess.run") as run:
            run.return_value.returncode = 0
            rc = server_main.main(["deploy"])
        self.assertEqual(0, rc)

        calls = [c.args[0] for c in run.call_args_list]
        venv_py = (fake_venv / "bin" / "python").as_posix()
        # 先建 venv（用当前解释器），再改用 venv python 重新执行 deploy，不直接跑真部署。
        self.assertEqual(
            calls,
            [
                [_sys.executable, "-m", "venv", fake_venv.as_posix()],
                [venv_py, "-m", "server.main", "deploy"],
            ],
        )
        # 说明首次一键部署无需预先 pip install 到默认环境。
        self.assertIn("不污染默认 Python", out.getvalue())


class ServerMainApiStatusTests(unittest.TestCase):
    """deploy 摘要里的 API 配置状态：按 config.raw 判断活动模型 api_key 是否就绪。"""

    def test_api_status_no_models(self):
        self.assertIn("未配置模型", server_main._api_config_status({}))
        self.assertIn("未配置模型", server_main._api_config_status({"models": []}))

    def test_api_status_active_model_without_key(self):
        raw = {"models": [{"name": "m1", "api_key": ""}], "current_model": "m1"}
        self.assertIn("api_key 为空", server_main._api_config_status(raw))

    def test_api_status_active_model_with_key(self):
        raw = {"models": [{"name": "m1", "api_key": "sk-x"}], "current_model": "m1"}
        self.assertIn("已配置 api_key", server_main._api_config_status(raw))

    def test_api_status_falls_back_to_first_model_when_current_missing(self):
        raw = {"models": [{"name": "m1", "api_key": "sk-x"}, {"name": "m2"}], "current_model": "ghost"}
        self.assertIn("m1", server_main._api_config_status(raw))


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
        today = today_utc8()
        store = Store(self.data_dir / "state.json")
        store.append_entries(
            "import",
            [{
                "entry_id": "a-1",
                "date": today,
                "ts": 1788652800000,
                "tag": "",
                "text": "hello",
            }],
        )
        with patch("sys.stdout", io.StringIO()):
            rc = server_main.main(["render"])
        self.assertEqual(0, rc)
        rendered = (self.data_dir / "Records" / f"{today}.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("hello", rendered)
        self.assertIn("a-1", rendered)

    def test_import_records_copies_files_whole(self):
        """方案 B：import 是**整文件拷贝**，不解析成条目、不重排（旧格式原样保留）。"""
        src = self.root / "records-import"
        src.mkdir()
        legacy_content = (
            "# 2024-01-01\n\n<summary>\n暂无今日总结。\n</summary>\n\n---\n"
            "## 原始记录流\n\n**08:00:** 旧记录\n"
        )
        (src / "2024-01-01.md").write_text(legacy_content, encoding="utf-8")
        # 非日期文件应被跳过
        (src / "notes.md").write_text("不是日记", encoding="utf-8")

        with patch("sys.stdout", io.StringIO()):
            rc = server_main.main(["import", "--records", str(src)])
        self.assertEqual(0, rc)

        store = Store(self.data_dir / "state.json")
        # 整文件拷贝：不生成条目态（历史日以整文件为权威）
        self.assertEqual(0, len(store.data["entries"]))
        # 源文件被原样拷贝到 Records/（含旧格式、summary）
        rendered = (self.data_dir / "Records" / "2024-01-01.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(legacy_content, rendered)
        # 非法日期文件被跳过
        self.assertFalse((self.data_dir / "Records" / "notes.md").exists())

    def test_import_preserves_today_file(self):
        """import 是整文件拷贝；若含“今天”文件，不得被 render_records 用空 state 覆盖。"""
        src = self.root / "records-import-today"
        src.mkdir()
        today = today_utc8()
        today_content = (
            f"# {today}\n\n<summary>\n今日总结\n</summary>\n\n---\n"
            "## 原始记录流\n\n**10:00:** 今天的一条记录\n"
        )
        (src / f"{today}.md").write_text(today_content, encoding="utf-8")
        with patch("sys.stdout", io.StringIO()):
            rc = server_main.main(["import", "--records", str(src)])
        self.assertEqual(0, rc)
        rendered = (self.data_dir / "Records" / f"{today}.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(today_content, rendered)

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