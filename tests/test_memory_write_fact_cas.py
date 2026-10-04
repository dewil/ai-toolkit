"""Unexpected fact publication is refused without touching the index."""
import importlib.util
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "memory-write.py"
SPEC = importlib.util.spec_from_file_location("memory_writer_fact_cas", SCRIPT)
WRITER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WRITER)


class UnexpectedFactCAS(unittest.TestCase):
    def test_unexpected_fact_after_absence_check_is_preserved(self):
        with tempfile.TemporaryDirectory(prefix="fact-cas-") as directory:
            memory = Path(directory)
            index = memory / "MEMORY.md"
            original_index = b"OLD_INDEX\n"
            unexpected_fact = b"UNEXPECTED_OTHER_FACT\n"
            index.write_bytes(original_index)
            original_atomic = WRITER.atomic_replace

            def hook(path, data, mode, *args, **kwargs):
                if path.name == "alpha.md":
                    path.write_bytes(unexpected_fact)
                return original_atomic(path, data, mode, *args, **kwargs)

            WRITER.atomic_replace = hook
            try:
                with self.assertRaises(WRITER.Invalid):
                    WRITER.write_fact(memory, index, memory / ".write.lock",
                                      "alpha", "Synthetic", b"Synthetic body\n")
            finally:
                WRITER.atomic_replace = original_atomic

            self.assertEqual((memory / "alpha.md").read_bytes(), unexpected_fact,
                             "unexpected fact was overwritten")
            self.assertEqual(index.read_bytes(), original_index,
                             "index changed after fact conflict")


if __name__ == "__main__":
    unittest.main()
