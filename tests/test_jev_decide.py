"""Слепые контрактные тесты DP-TECH-06–10; сеть заменена на границе urllib.

Запуск: python3 -m pytest -q tests/test_jev_decide.py
Новые требования к stderr отделены от проверок неизменного контракта.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import selectors
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "jev-decide.py"
KEY = "sk-or-v1-" + "a" * 64
PROXY = "http://dp_user:dp_password@proxy.invalid:18443"
QUESTIONS = {"public": {"type": "noul", "instructions": "Is this public?"}}
COMPLETE = {"answers": {"public": {"type": "noul", "noul": 0.75}}}
# cli, environment, expected handler value, expected source
SOURCES = {
    "none": (None, {}, "", "не задан"),
    "cli": (PROXY, {}, PROXY, "--proxy"),
    "jev": (None, {"JEV_PROXY": PROXY}, PROXY, "JEV_PROXY"),
    "upper": (None, {"HTTPS_PROXY": PROXY}, PROXY, "HTTPS_PROXY"),
    "lower": (None, {"https_proxy": PROXY}, PROXY, "HTTPS_PROXY"),
    "cli_wins": (PROXY, {"JEV_PROXY": "http://jev.invalid:1", "HTTPS_PROXY": "http://upper.invalid:2", "https_proxy": "http://lower.invalid:3"}, PROXY, "--proxy"),
    "jev_wins": (None, {"JEV_PROXY": PROXY, "HTTPS_PROXY": "http://upper.invalid:2", "https_proxy": "http://lower.invalid:3"}, PROXY, "JEV_PROXY"),
    "upper_wins": (None, {"HTTPS_PROXY": PROXY, "https_proxy": "http://lower.invalid:3"}, PROXY, "HTTPS_PROXY"),
    "empty_cli": ("", {"JEV_PROXY": PROXY}, PROXY, "JEV_PROXY"),
    "empty_jev": (None, {"JEV_PROXY": "", "HTTPS_PROXY": PROXY}, PROXY, "HTTPS_PROXY"),
    "empty_upper": (None, {"HTTPS_PROXY": "", "https_proxy": PROXY}, PROXY, "HTTPS_PROXY"),
    "all_empty": ("", {"JEV_PROXY": "", "HTTPS_PROXY": "", "https_proxy": ""}, "", "не задан"),
}
ERRORS = ("html403", "http429", "network", "deadline")
RESPONSES = {
    "complete": COMPLETE,
    "incomplete": {"answers": {}},
    "unrecognized": {"unexpected": "synthetic response"},
}


class Contract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.questions = self.root / "questions.json"
        self.questions.write_text(json.dumps(QUESTIONS), encoding="utf-8")
        self.state = self.root / "state.txt"
        self.state.write_text("A public synthetic announcement", encoding="utf-8")
        # Import with a synthetic home, never read the user's real key file.
        with mock.patch.object(Path, "home", return_value=self.root):
            spec = importlib.util.spec_from_file_location("jev_decide_contract", SCRIPT)
            self.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.module)

    def run_cli(self, source="none", outcome="complete", local=None, reflect=False):
        self.questions.write_text(json.dumps(QUESTIONS), encoding="utf-8")
        self.state.write_text("A public synthetic announcement", encoding="utf-8")
        cli, env, selected, label = SOURCES[source]
        env = dict(env, OPENROUTER_API_KEY=KEY)
        argv = [str(SCRIPT), "--questions", str(self.questions), "--state-file", str(self.state)]
        if cli is not None:
            argv += ["--proxy", cli]
        if local == "json":
            self.questions.write_text("{invalid", encoding="utf-8")
        elif local == "empty":
            self.state.write_text("", encoding="utf-8")
        elif local == "key":
            env.pop("OPENROUTER_API_KEY")
        elif local == "stdin":
            del argv[3:5]
        elif local == "help":
            argv = [str(SCRIPT), "--help"]
        elif local == "syntax":
            argv = [str(SCRIPT), "--definitely-unknown"]
        stdout, stderr = io.StringIO(), io.StringIO()
        seen = {}
        response = dict(RESPONSES.get(outcome, COMPLETE))
        if reflect:
            response["echo"] = f"{KEY} {PROXY}"

        class Input(io.StringIO):
            def read(inner, *args, **kwargs):
                seen["stdin"] = stderr.getvalue()
                return super().read(*args, **kwargs)

        def open_request(request, **kwargs):
            seen["request"] = stderr.getvalue()
            if outcome == "html403":
                raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {"Content-Type": "text/html"}, io.BytesIO(f"<html>Cloudflare synthetic denial {KEY} {PROXY}</html>".encode()))
            if outcome == "http429":
                raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {}, io.BytesIO(json.dumps({"error": f"synthetic limit {KEY} {PROXY}"}).encode()))
            if outcome == "network":
                raise urllib.error.URLError(f"synthetic offline {KEY} {PROXY}")
            return contextlib.closing(io.BytesIO(json.dumps(response).encode()))

        original_read_text = Path.read_text

        def read_input(path, *args, **kwargs):
            if path in (self.questions, self.state, self.module.KEY_FILE):
                seen[f"read {path.name}"] = stderr.getvalue()
            return original_read_text(path, *args, **kwargs)

        opener = mock.Mock()
        opener.open.side_effect = open_request
        before = sorted(p.relative_to(self.root) for p in self.root.rglob("*"))
        old_cwd = Path.cwd()
        with contextlib.ExitStack() as stack:
            stack.callback(os.chdir, old_cwd)
            os.chdir(self.root)
            stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
            stack.enter_context(mock.patch.object(sys, "argv", argv))
            stack.enter_context(mock.patch.object(sys, "stdin", Input("Synthetic public stdin")))
            stack.enter_context(contextlib.redirect_stdout(stdout))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            stack.enter_context(mock.patch.object(socket.socket, "connect", side_effect=AssertionError("Real network forbidden")))
            stack.enter_context(mock.patch.object(Path, "read_text", read_input))
            build = stack.enter_context(mock.patch("urllib.request.build_opener", return_value=opener))
            if outcome == "deadline":
                worker = stack.enter_context(mock.patch.object(self.module.threading, "Thread"))
                worker.return_value.is_alive.return_value = True
            try:
                result = self.module.main()
                code = result if isinstance(result, int) else 0
            except SystemExit as exc:
                code = exc.code
                if not isinstance(code, (int, type(None))):
                    print(code, file=stderr)
                    code = 1
                elif code is None:
                    code = 0
            if outcome == "deadline":
                worker.return_value.start.assert_called_once()
                worker.return_value.join.assert_called_once_with(self.module.DEADLINE)
        self.assertEqual(before, sorted(p.relative_to(self.root) for p in self.root.rglob("*")), "Unexpected diagnostic files")
        if local in ("json", "empty", "key", "help", "syntax"):
            opener.open.assert_not_called()
        else:
            build.assert_called_once()
            handlers = [h for h in build.call_args.args if hasattr(h, "proxies")]
            self.assertEqual(len(handlers), 1)
            self.assertEqual(handlers[0].proxies, {"https": selected, "http": selected} if selected else {})
            if outcome != "deadline":
                opener.open.assert_called_once()
        for secret in (KEY, PROXY, "dp_user", "dp_password"):
            self.assertNotIn(secret, stderr.getvalue())
        return code, stdout.getvalue(), stderr.getvalue(), seen, label

    def assert_source(self, result):
        code, out, err, seen, label = result
        line = f"прокси: {label}"
        self.assertEqual(err.splitlines().count(line), 1, err)
        self.assertEqual(err.splitlines()[0], line, err)
        for phase, diagnostic in seen.items():
            self.assertEqual(diagnostic.splitlines().count(line), 1, f"Source missing before {phase}: {diagnostic!r}")
        for secret in (KEY, PROXY, "dp_user", "dp_password", "proxy.invalid", "18443"):
            self.assertNotIn(secret, err)

    def test_unchanged_local_failures_and_argparse(self):
        """DP-TECH-09: прежние коды, локальный отказ без сети."""
        for local, expected in (("json", 1), ("empty", 1), ("key", 1), ("help", 0), ("syntax", 2)):
            with self.subTest(local=local):
                code, out, err, _, _ = self.run_cli(local=local)
                self.assertEqual(code, expected)
                if local == "help":
                    self.assertIn("--questions", out)
                else:
                    self.assertEqual(out, "")
                    self.assertTrue(err.strip())
                    if local in ("json", "empty", "key"):
                        self.assertIn({"json": "вопрос", "empty": "пустой", "key": "ключ"}[local], err.lower())

    def test_unchanged_reflected_secrets_are_masked(self):
        """DP-TECH-10: маскировка ответа остается в силе."""
        code, out, err, _, _ = self.run_cli(source="cli", reflect=True)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["answers"], COMPLETE["answers"])
        for secret in (KEY, PROXY, "dp_password"):
            self.assertNotIn(secret, out + err)

    def test_source_before_stdin_read(self):
        """DP-TECH-09: строка уже выдана к моменту чтения stdin."""
        self.assert_source(self.run_cli(local="stdin"))

    def test_source_with_reflected_secrets(self):
        """DP-TECH-10: источник ровно один раз, без адреса и credentials."""
        self.assert_source(self.run_cli(source="cli", reflect=True))

    def test_source_is_flushed_while_waiting_for_stdin(self):
        """DP-TECH-09: реальный pipe stderr, stdin пока не закрыт."""
        # No -u / PYTHONUNBUFFERED: observe ordinary CLI buffering.
        harness = "import runpy,socket,sys\ndef deny(*a,**k): raise AssertionError('Real network forbidden')\nsocket.socket.connect=deny\ntarget=sys.argv.pop(1)\nsys.argv[0]=target\nrunpy.run_path(target,run_name='__main__')"
        env = {"HOME": str(self.root), "OPENROUTER_API_KEY": KEY, "PYTHONIOENCODING": "utf-8"}
        with subprocess.Popen([sys.executable, "-c", harness, str(SCRIPT), "--questions", str(self.questions)], cwd=self.root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(proc.stderr, selectors.EVENT_READ)
                    ready = selector.select(timeout=5)
                    self.assertTrue(ready, "No source diagnostic while CLI waits for stdin")
                    diagnostic = os.read(proc.stderr.fileno(), 4096).decode("utf-8")
                self.assertEqual(diagnostic.splitlines(), ["прокси: не задан"])
                self.assertIsNone(proc.poll(), "CLI should still be waiting for input")
            finally:
                proc.kill()
                proc.communicate(timeout=5)


def source_test(name, new):
    def test(self):
        result = self.run_cli(source=name)
        self.assertEqual(result[0], 0)
        self.assertEqual(json.loads(result[1]), COMPLETE)
        if new:
            self.assert_source(result)
    test.__doc__ = "DP-TECH-06/07/10: " + name
    return test


def outcome_test(outcome, new):
    def test(self):
        result = self.run_cli(source="cli", outcome=outcome)
        self.assertEqual(result[0], 1 if outcome in ERRORS else 3)
        if outcome in ERRORS:
            self.assertEqual(result[1], "")
            self.assertTrue(result[2].strip())
            if outcome == "html403":
                self.assertIn("403", result[2])
                self.assertRegex(result[2].lower(), r"html|cloudflare|шлюз")
        else:
            self.assertEqual(json.loads(result[1]), RESPONSES[outcome])
        if new:
            self.assert_source(result)
    test.__doc__ = "DP-TECH-08/10: " + outcome
    return test


def local_test(local):
    def test(self):
        result = self.run_cli(local=local)
        self.assertEqual(result[0], 1)
        self.assert_source(result)
        self.assertGreater(len(result[2].splitlines()), 1, "Must retain local error diagnostic")
    test.__doc__ = "DP-TECH-09: источник до локального отказа " + local
    return test


for _name in SOURCES:
    for _new in (False, True):
        setattr(Contract, f"test_{'source' if _new else 'unchanged'}_proxy_{_name}", source_test(_name, _new))
for _outcome in (*ERRORS, "incomplete", "unrecognized"):
    for _new in (False, True):
        setattr(Contract, f"test_{'source' if _new else 'unchanged'}_outcome_{_outcome}", outcome_test(_outcome, _new))
for _local in ("json", "empty", "key"):
    setattr(Contract, f"test_source_before_local_{_local}", local_test(_local))


if __name__ == "__main__":
    unittest.main()
