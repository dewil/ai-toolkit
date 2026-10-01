#!/usr/bin/env python3
"""FR-AIB-08: legacy writers cannot enter new-layout or bootstrap roots."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
spec = importlib.util.spec_from_file_location("canon_delta", SCRIPTS / "canon-delta.py")
cd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cd)


def snapshot(root):
    return {str(p.relative_to(root)): ("link", os.readlink(p)) if p.is_symlink()
            else ("dir",) if p.is_dir() else ("file", p.read_bytes())
            for p in root.rglob("*")}


class LegacyGuardTest(unittest.TestCase):
    def test_reserved_layouts_block_all_writers_without_changes(self):
        for marker in (".AI", ".ai-bootstrap"):
            for kind in ("directory", "file", "dangling-link"):
                for operation in ("recover", "sync", "migrate", "library-recover", "library-apply"):
                    with self.subTest(marker=marker, kind=kind, operation=operation), tempfile.TemporaryDirectory() as tmp:
                        base = Path(tmp)
                        root = base / "project"
                        root.mkdir()
                        entry = root / marker
                        if kind == "directory":
                            entry.mkdir()
                            (entry / "user-data").write_text("preserve me")
                        elif kind == "file":
                            entry.write_text("preserve me")
                        else:
                            entry.symlink_to(base / "missing")
                        intent = base / "intent.yaml"
                        intent.write_text("project_type: [universal]\n")
                        descriptor = {"schema_version": 1, "min_cli_version": 1,
                                      "manifest_digest": "digest", "files": {}, "membership": {},
                                      "plugin_source": None}
                        lock = base / "lock.json"
                        lock.write_text(json.dumps(descriptor))
                        old = base / "canon.yaml"
                        old.write_text("project_type: [universal]\nfiles:\n  - rules/a.md\n")
                        before = snapshot(root)
                        if operation.startswith("library-"):
                            with self.assertRaises(SystemExit) as error:
                                if operation == "library-recover":
                                    cd.recover(root)
                                else:
                                    cd.apply_release(root, {"project_type": ["universal"]},
                                                     cd.empty_state(), descriptor, "a" * 40,
                                                     cd.DictBlobSource({}), root / ".claude/canon.state.json")
                            self.assertNotEqual(error.exception.code, 0)
                        else:
                            script = "canon-migrate.py" if operation == "migrate" else "canon-delta.py"
                            args = [sys.executable, str(SCRIPTS / script), "--root", str(root)]
                            if operation == "migrate":
                                args += ["--canon", str(old), "--force"]
                            elif operation == "sync":
                                args += ["sync", "--intent", str(intent), "--lock", str(lock),
                                         "--mirror", str(base), "--target", "a" * 40]
                            else:
                                args += ["recover"]
                            result = subprocess.run(args, capture_output=True, text=True)
                            self.assertNotEqual(result.returncode, 0, result.stdout)
                            self.assertIn(marker, result.stderr)
                        self.assertEqual(snapshot(root), before)

    def test_legacy_recover_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".claude").mkdir()
            self.assertEqual(cd.recover(root), {"status": "clean"})


if __name__ == "__main__":
    unittest.main()
