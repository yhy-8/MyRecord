"""Bounded application logging without journal or model payloads."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import settings


LOG_NAME = "AgentRecord.log"
MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 2
_HANDLER_NAME = "AgentRecord.rotating_file"


def configure_logging(
    log_dir: Path | None = None,
    *,
    max_bytes: int = MAX_LOG_BYTES,
    backup_count: int = LOG_BACKUP_COUNT,
    force: bool = False,
) -> Path | None:
    """Configure one size-rotated handler for the AgentRecord logger tree."""
    directory = log_dir or settings.LOG_DIR
    path = directory / LOG_NAME
    logger = logging.getLogger("AgentRecord")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    for handler in list(logger.handlers):
        if handler.get_name() != _HANDLER_NAME:
            continue
        same_path = Path(getattr(handler, "baseFilename", "")) == path.resolve()
        if same_path and not force:
            return path
        logger.removeHandler(handler)
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
    logger.addHandler(handler)
    return path
