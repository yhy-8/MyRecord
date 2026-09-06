"""server/hub/backup.py：由服务端内置调度的每周数据备份。

备份作为子进程执行 server/deploy/backup.sh；上次成功时间持久化在 data 目录
`.backup-state.json`。测试用假锁与假的 subprocess.run，不真正触发备份脚本。
"""

import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.hub import backup
from server.hub.atomic_write import atomic_write


class _FakeLock:
    def release(self):
        pass


class _FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class BackupSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self.tmp.cleanup)
        self.lock_patch = patch("server.hub.backup.FileLock.acquire", return_value=_FakeLock())
        self.lock_patch.start()
        self.addCleanup(self.lock_patch.stop)

    def _run_patch(self, result=None):
        run = patch("server.hub.backup.subprocess.run", return_value=result or _FakeResult())
        mock = run.start()
        self.addCleanup(run.stop)
        return mock

    def _state(self):
        path = self.data_dir / ".backup-state.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_state(self, last_backup_at: str):
        atomic_write(
            self.data_dir / ".backup-state.json",
            json.dumps({"last_backup_at": last_backup_at}, ensure_ascii=False),
        )

    def test_backup_script_resolves_to_deploy_dir(self):
        expected = Path(backup.__file__).resolve().parent.parent / "deploy" / "backup.sh"
        self.assertEqual(backup._backup_script(), expected)

    def test_due_when_never_backed_up_runs_script_and_records_time(self):
        run = self._run_patch(_FakeResult(returncode=0, stdout="已备份"))
        backup.run_backup_if_due(self.data_dir)
        run.assert_called_once()
        self.assertTrue(self._state()["last_backup_at"])

    def test_not_due_within_interval(self):
        self._write_state(datetime.datetime.now().isoformat(timespec="seconds"))
        run = self._run_patch()
        backup.run_backup_if_due(self.data_dir)
        run.assert_not_called()

    def test_due_after_interval(self):
        old = datetime.datetime.now() - datetime.timedelta(days=8)
        self._write_state(old.isoformat(timespec="seconds"))
        run = self._run_patch(_FakeResult(returncode=0))
        backup.run_backup_if_due(self.data_dir)
        run.assert_called_once()
        self.assertTrue(self._state()["last_backup_at"])

    def test_skips_when_script_missing(self):
        with patch("server.hub.backup._backup_script", return_value=self.data_dir / "nope.sh"):
            run = self._run_patch()
            backup.run_backup_if_due(self.data_dir)
        run.assert_not_called()

    def test_failure_records_last_error_without_updating_time(self):
        run = self._run_patch(_FakeResult(returncode=1, stderr="boom"))
        backup.run_backup_if_due(self.data_dir)
        run.assert_called_once()
        state = self._state()
        self.assertNotIn("last_backup_at", state)
        self.assertEqual("boom", state["last_error"])


if __name__ == "__main__":
    unittest.main()
