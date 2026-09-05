"""Bounded application logging without journal or model payloads."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import settings


LOG_NAME = "MyRecord.log"
MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 2
_HANDLER_NAME = "MyRecord.rotating_file"

# 服务端代码通过 logger=getLogger(__name__) 落在 "server.*" 树（含 hub 与 ai）；
# 另一些代码直接用 "MyRecord" 根 logger。单一 handler 同时挂到这两个根上，避免
# AI 分析流程与同步请求的日志被静默丢弃。
_LOGGER_ROOTS = ("MyRecord", "server")


def configure_logging(
    log_dir: Path | None = None,
    *,
    max_bytes: int = MAX_LOG_BYTES,
    backup_count: int = LOG_BACKUP_COUNT,
    force: bool = False,
) -> Path | None:
    """Configure one size-rotated handler for the server logger roots."""
    directory = log_dir or settings.LOG_DIR
    path = directory / LOG_NAME
    roots = [logging.getLogger(name) for name in _LOGGER_ROOTS]

    # 已存在指向同一文件的 handler 且未要求强制重建时直接复用。
    for root in roots:
        for handler in list(root.handlers):
            if handler.get_name() != _HANDLER_NAME:
                continue
            same_path = Path(getattr(handler, "baseFilename", "")) == path.resolve()
            if same_path and not force:
                return path

    # 先移除可能已存在的同名单一 handler，避免重复挂载造成重复写/重复滚动。
    for root in roots:
        for handler in list(root.handlers):
            if handler.get_name() == _HANDLER_NAME:
                root.removeHandler(handler)
                handler.close()

    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
    except OSError:
        return None
    for archive in directory.glob(f"{LOG_NAME}.*"):
        suffix = archive.name.removeprefix(f"{LOG_NAME}.")
        if suffix.isdigit() and int(suffix) > backup_count:
            try:
                archive.unlink()
            except OSError:
                pass
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    for root in roots:
        root.setLevel(logging.INFO)
        root.propagate = False
        root.addHandler(handler)
    return path
