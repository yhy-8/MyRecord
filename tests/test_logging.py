import logging
import tempfile
import unittest
from pathlib import Path

from server.ai.logging_config import configure_logging


class LoggingTests(unittest.TestCase):
    def test_log_rotates_by_size_and_keeps_bounded_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            stale_archive = log_dir / "AgentRecord.log.3"
            stale_archive.write_text("old", encoding="utf-8")
            path = configure_logging(
                log_dir, max_bytes=200, backup_count=2, force=True
            )
            logger = logging.getLogger("AgentRecord.test")
            for index in range(20):
                logger.info("rotation_check index=%s padding=%s", index, "x" * 80)
            for handler in logging.getLogger("AgentRecord").handlers:
                handler.flush()

            files = list(log_dir.glob("AgentRecord.log*"))
            self.assertEqual(log_dir / "AgentRecord.log", path)
            self.assertLessEqual(len(files), 3)
            self.assertTrue((log_dir / "AgentRecord.log.1").exists())
            self.assertFalse(stale_archive.exists())

            application_logger = logging.getLogger("AgentRecord")
            for handler in list(application_logger.handlers):
                if getattr(handler, "baseFilename", None):
                    application_logger.removeHandler(handler)
                    handler.close()


if __name__ == "__main__":
    unittest.main()