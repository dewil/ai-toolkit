#!/usr/bin/env python3
"""Независимый контракт host collector; только временные canary-файлы."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("collector_contract_run_evals", ROOT / "scripts/run-evals.py")
collector = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = collector
_spec.loader.exec_module(collector)

# Python's documented audit event runs before open, regardless of whether the
# caller uses pathlib, io.open, or os.open. No private collector seam is assumed.
_pending_open_probe = None

def _before_open(event, args):
    global _pending_open_probe
    if event == "open" and _pending_open_probe is not None:
        path = args[0]
        if isinstance(path, (str, bytes, os.PathLike)) and os.fsdecode(path).split("/")[-1] == "note.txt":
            action = _pending_open_probe
            _pending_open_probe = None
            action()

sys.addaudithook(_before_open)


def digest(data):
    return hashlib.sha256(data).hexdigest()


class CollectorContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "root"
        self.root.mkdir()
        self.outside = self.base / "outside"
        self.outside.mkdir()
        self.canary = b"TEMP-ONLY-COLLECTOR-CANARY-9f51\n"
        (self.outside / "note.txt").write_bytes(self.canary)
        (self.root / "good.txt").write_text("обычный\n", encoding="utf-8")

    def collect(self, method):
        return getattr(collector, method)(self.root)

    def assert_only_good(self, method):
        expected = "обычный\n" if method == "capture" else digest("обычный\n".encode())
        self.assertEqual(self.collect(method), {"good.txt": expected})

    def test_normal_files_hash_text_and_capture_limits(self):
        (self.root / "nested").mkdir()
        data = "Привет, мир\n".encode()
        (self.root / "nested" / "utf8.txt").write_bytes(data)
        (self.root / "binary").write_bytes(b"\xff\x00\xfe")
        (self.root / "at-limit").write_bytes(b"a" * (64 * 1024))
        (self.root / "over-limit").write_bytes(b"b" * (64 * 1024 + 1))
        self.assertEqual(collector.MAX_CAPTURE, 64 * 1024)
        files = {"good.txt": "обычный\n".encode(), "nested/utf8.txt": data,
                 "binary": b"\xff\x00\xfe", "at-limit": b"a" * (64 * 1024),
                 "over-limit": b"b" * (64 * 1024 + 1)}
        self.assertEqual(self.collect("snapshot"), {p: digest(b) for p, b in files.items()})
        self.assertEqual(self.collect("capture"), {p: files[p].decode() for p in
                         ("good.txt", "nested/utf8.txt", "at-limit")})

    def test_git_and_internal_home_are_excluded(self):
        for prefix in (".git", ".eval-home", "nested/.git", "nested/.eval-home"):
            path = self.root / prefix
            path.mkdir(parents=True)
            (path / "private.txt").write_bytes(self.canary)
        for method in ("snapshot", "capture"):
            with self.subTest(method=method):
                self.assert_only_good(method)

    def test_capture_preserves_universal_newline_text_behavior(self):
        (self.root / "lines.txt").write_bytes(b"line1\r\nline2\rline3\n")
        self.assertEqual(self.collect("capture")["lines.txt"], "line1\nline2\nline3\n")

    def test_missing_or_zero_security_flags_fail_closed(self):
        for flag in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK"):
            for missing in (False, True):
                for method in ("snapshot", "capture"):
                    with self.subTest(flag=flag, missing=missing, method=method):
                        with mock.patch.dict(collector.os.__dict__):
                            if missing:
                                collector.os.__dict__.pop(flag, None)
                            else:
                                setattr(collector.os, flag, 0)
                            try:
                                result = self.collect(method)
                            except (OSError, ValueError, RuntimeError):
                                continue  # Explicit rejection is safe too.
                            self.assertEqual(result, {}, "unsupported primitives silently weakened collection")

    def test_symlink_files_absolute_relative_and_internal_are_skipped(self):
        for name, target in (("absolute", self.outside / "note.txt"),
                             ("relative", Path("../outside/note.txt")),
                             ("internal", Path("good.txt"))):
            (self.root / name).symlink_to(target)
        for method in ("snapshot", "capture"):
            with self.subTest(method=method):
                self.assert_only_good(method)

    def test_directory_link_broken_link_and_cycle_are_skipped(self):
        (self.root / "external-dir").symlink_to(self.outside, target_is_directory=True)
        (self.root / "broken").symlink_to("missing")
        (self.root / "cycle").symlink_to(".", target_is_directory=True)
        for method in ("snapshot", "capture"):
            with self.subTest(method=method):
                self.assert_only_good(method)

    def test_root_symlink_is_rejected_without_canary(self):
        link = self.base / "root-link"
        link.symlink_to(self.outside, target_is_directory=True)
        for method in ("snapshot", "capture"):
            with self.subTest(method=method):
                try:
                    result = getattr(collector, method)(link)
                except (OSError, ValueError):
                    continue  # Explicit rejection and empty collection are both safe.
                self.assertEqual(result, {})

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO")
    def test_fifo_and_socket_are_skipped_without_hanging(self):
        os.mkfifo(self.root / "fifo")
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sock.bind(str(self.root / "socket"))
        # A broken collector may block in open(FIFO). Bound each invocation and
        # kill/reap it through subprocess.run rather than hanging the test suite.
        for method in ("snapshot", "capture"):
            with self.subTest(method=method):
                code = ("import importlib.util,sys,json; from pathlib import Path; "
                        "s=importlib.util.spec_from_file_location('probe',sys.argv[1]); "
                        "m=importlib.util.module_from_spec(s); sys.modules[s.name]=m; "
                        "s.loader.exec_module(m); print(json.dumps(getattr(m,sys.argv[2])(Path(sys.argv[3]))))")
                try:
                    done = subprocess.run([sys.executable, "-c", code,
                                           str(ROOT / "scripts/run-evals.py"), method, str(self.root)],
                                          capture_output=True, text=True, timeout=3, check=True)
                except subprocess.TimeoutExpired:
                    self.fail(f"{method} blocked on a special file")
                expected = "обычный\n" if method == "capture" else digest("обычный\n".encode())
                self.assertEqual(json.loads(done.stdout), {"good.txt": expected})

    def run_swap_probe(self, method, directory=False, fifo=False, disappear=False):
        with tempfile.TemporaryDirectory() as tmp:
            previous_root = self.root
            self.root = Path(tmp)
            (self.root / "good.txt").write_text("обычный\n", encoding="utf-8")
            try:
                self._swap_probe(method, directory, fifo, disappear)
            finally:
                self.root = previous_root

    def _swap_probe(self, method, directory, fifo, disappear):
        global _pending_open_probe
        leafdir = self.root / "nested"
        leafdir.mkdir(exist_ok=True)
        leaf = leafdir / "note.txt"
        leaf.write_text("trusted-before-swap\n", encoding="utf-8")
        fired = []
        def swap():
            fired.append(True)
            if directory:
                leafdir.rename(self.root / "held-directory")
                leafdir.symlink_to(self.outside, target_is_directory=True)
            else:
                leaf.unlink()
                if fifo:
                    os.mkfifo(leaf)
                elif not disappear:
                    leaf.symlink_to(self.outside / "note.txt")
        _pending_open_probe = swap
        try:
            result = self.collect(method)
        finally:
            _pending_open_probe = None
        self.assertTrue(fired, "public pre-open probe did not run; review must supply another fault probe")
        expected_good = "обычный\n" if method == "capture" else digest("обычный\n".encode())
        self.assertEqual(result.get("good.txt"), expected_good)
        trusted = "trusted-before-swap\n" if method == "capture" else digest(b"trusted-before-swap\n")
        allowed = {trusted} if directory else set()
        for name, value in result.items():
            if name == "good.txt":
                continue
            self.assertIn(value, allowed, "untrusted/replaced or unread file was reported as successfully read")
        self.assertNotIn(self.canary.decode(), result.values())
        self.assertNotIn(digest(self.canary), result.values())

    def test_file_swap_to_symlink_before_open(self):
        for method in ("snapshot", "capture"):
            with self.subTest(method=method):
                self.run_swap_probe(method)

    def test_directory_swap_to_symlink_before_leaf_open(self):
        for method in ("snapshot", "capture"):
            with self.subTest(method=method):
                self.run_swap_probe(method, directory=True)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO")
    def test_file_swap_to_fifo_before_open_is_safe_and_bounded(self):
        for method in ("snapshot", "capture"):
            with self.subTest(method=method):
                code = ("import importlib.util,sys; from pathlib import Path; "
                        "s=importlib.util.spec_from_file_location('contract',sys.argv[1]); "
                        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                        "c=m.CollectorContract(); c.root=Path(sys.argv[3]); "
                        "c.outside=Path(sys.argv[4]); c.canary=(c.outside/'note.txt').read_bytes(); "
                        "c._swap_probe(sys.argv[2],False,True,False)")
                with tempfile.TemporaryDirectory() as tmp:
                    probe_root = Path(tmp)
                    (probe_root / "good.txt").write_text("обычный\n", encoding="utf-8")
                    try:
                        done = subprocess.run([sys.executable, "-c", code, str(Path(__file__).resolve()),
                                               method, str(probe_root), str(self.outside)],
                                              capture_output=True, text=True, timeout=3)
                    except subprocess.TimeoutExpired:
                        self.fail(f"{method} blocked after a regular file became a FIFO")
                    self.assertEqual(done.returncode, 0, done.stderr)

    def test_disappearance_before_open_does_not_become_empty_success(self):
        for method in ("snapshot", "capture"):
            with self.subTest(method=method):
                self.run_swap_probe(method, disappear=True)

    def test_link_cannot_satisfy_file_assertions(self):
        (self.root / "linked.txt").symlink_to(self.outside / "note.txt")
        run = collector.Run()
        run.files = self.collect("snapshot")
        run.contents = self.collect("capture")
        for assertion in ({"file_exists": {"path": "linked.txt"}},
                          {"file_matches": {"path": "linked.txt", "pattern": "CANARY"}},
                          {"file_not_matches": {"path": "linked.txt", "pattern": "unrelated"}}):
            with self.subTest(assertion=assertion):
                self.assertFalse(collector.check(assertion, run, {})[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
