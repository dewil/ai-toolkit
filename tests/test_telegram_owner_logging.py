#!/usr/bin/env python3
"""Keep CLI logging isolated from library loggers and existing root handlers."""
from pathlib import Path
import subprocess
import sys
import unittest


class Logging(unittest.TestCase):
    def test_main_configures_only_owner_logger(self):
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        program = """
import io
import logging
import telegram_session_owner as owner
root = logging.getLogger()
root_stream = io.StringIO()
if configured:
    root.setLevel(logging.ERROR)
    root.addHandler(logging.StreamHandler(root_stream))
    root.addFilter(logging.Filter())
before = (root.level, root.handlers[:], root.filters[:])
assert owner.main(['--instance', 'a', '--config', '/missing-owner-config']) == 2
assert (root.level, root.handlers, root.filters) == before, 'root logger changed'
logger = logging.getLogger(owner.__name__)
assert logger.handlers, 'owner handler missing'
assert logger.level == logging.INFO
assert not logger.propagate
logger.info('OWNER_LOGGING_TEST')
assert not root_stream.getvalue(), 'owner log propagated to root'
"""
        for configured in (False, True):
            with self.subTest(root_configured=configured):
                result = subprocess.run(
                    [sys.executable, "-c", f"configured = {configured!r}\n" + program],
                    cwd=scripts, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr.splitlines(), [
                    "INVALID_CONFIG", "INFO telegram_session_owner OWNER_LOGGING_TEST"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
