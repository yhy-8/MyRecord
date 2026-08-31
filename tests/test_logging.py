import logging
import tempfile
import unittest
from pathlib import Path

from server.ai.logging_config import configure_logging


class LoggingTests(unittest.TestCase):
    def test_log_rotates_by_size_and_keeps_bounded_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            stale_archive = log_dir / "MyRecord.log.3"
            stale_archive.write_text("old", encoding="utf-8")
            path = configure_logging(
                log_dir, max_bytes=200, backup_count=2, force=True
            )
            logger = logging.getLogger("MyRecord.test")
            for index in range(20):
                logger.info("rotation_check index=%s padding=%s", index, "x" * 80)
            for handler in logging.getLogger("MyRecord").handlers:
                handler.flush()

            files = list(log_dir.glob("MyRecord.log*"))
            self.assertEqual(log_dir / "MyRecord.log", path)
            self.assertLessEqual(len(files), 3)
            self.assertTrue((log_dir / "MyRecord.log.1").exists())
            self.assertFalse(stale_archive.exists())

            # configure_logging 把同一 handler 挂到 MyRecord 与 server 两个根；全部清掉，
            # 避免残留指向已删除临时目录的 handler 污染后续 e2e 测试。
            for root_name in ("MyRecord", "server"):
                root_logger = logging.getLogger(root_name)
                for handler in list(root_logger.handlers):
                    if getattr(handler, "baseFilename", None):
                        root_logger.removeHandler(handler)
                        handler.close()


if __name__ == "__main__":
    unittest.main()