"""数据空间定期备份：由服务端内置调度，免去独立的 systemd 备份单元/定时器。

服务端运行期间，后台调度线程每隔 60 秒检查一次是否已达备份周期（默认 7 天），
到期则以子进程执行 ``server/deploy/backup.sh``，把整个数据空间打成单个 gzip tar
快照并保留最近 N 份。上次成功备份时间持久化在 data 目录 ``.backup-state.json``；
服务端停止期间错过的备份会在下次启动后立即补跑（等效于 timer 的 ``Persistent=true``）。
"""

import datetime
import json
import logging
import subprocess
from pathlib import Path

from .atomic_write import atomic_write
from ..ai.file_lock import FileLock

logger = logging.getLogger(__name__)

BACKUP_INTERVAL_DAYS = 7
_BACKUP_STATE_FILE = ".backup-state.json"
_BACKUP_LOCK_FILE = ".backup.lock"


def _backup_script() -> Path:
    """server/deploy/backup.sh（随包位置推导，支持服务端工程改名）。"""
    return Path(__file__).resolve().parent.parent / "deploy" / "backup.sh"


def _state_path(data_dir: Path) -> Path:
    return data_dir / _BACKUP_STATE_FILE


def _load_state(data_dir: Path) -> dict:
    path = _state_path(data_dir)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def _save_state(data_dir: Path, state: dict) -> None:
    atomic_write(_state_path(data_dir), json.dumps(state, ensure_ascii=False, indent=2))


def _backup_due(state: dict, now: datetime.datetime) -> bool:
    last = state.get("last_backup_at", "")
    if not last:
        return True
    try:
        return now >= datetime.datetime.fromisoformat(last) + datetime.timedelta(
            days=BACKUP_INTERVAL_DAYS
        )
    except ValueError:
        return True


def run_backup_if_due(data_dir: Path) -> None:
    """若距上次成功备份已达 7 天（或从未备份）则执行一次备份，并记录时间。

    备份作为子进程（bash backup.sh）运行，与主进程隔离：失败/异常只记录日志、
    不中断服务；因 ``last_backup_at`` 未更新，下次检查仍视为「到期」而自然重试。
    backup.sh 自定位于其工程根并读取 config.yaml 的 data_dir，因此不依赖调用目录。
    """
    script = _backup_script()
    if not script.is_file():
        logger.warning("backup_script_missing path=%s", script)
        return
    lock = FileLock.acquire(data_dir / _BACKUP_LOCK_FILE)
    if lock is None:
        return
    try:
        state = _load_state(data_dir)
        now = datetime.datetime.now()
        if not _backup_due(state, now):
            return
        result = subprocess.run(
            ["bash", script.as_posix()], capture_output=True, text=True
        )
        if result.returncode == 0:
            state["last_backup_at"] = now.isoformat(timespec="seconds")
            state.pop("last_error", None)
            _save_state(data_dir, state)
            logger.info("backup_completed")
        else:
            detail = (result.stderr.strip() or result.stdout.strip() or "未知错误")
            state["last_error"] = detail
            _save_state(data_dir, state)
            logger.warning("backup_failed error=%s", detail)
    except Exception as error:
        logger.warning("backup_cycle_failed error_type=%s", error.__class__.__name__)
    finally:
        lock.release()
