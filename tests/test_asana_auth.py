"""Контракт AUTH-CLI/PRIORITY/FAILURE: временный HOME, только тестовые PAT."""
import builtins
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


class AsanaAuth(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "ASANA_TOKEN": " ENV-PAT "})
        env.start()
        self.addCleanup(env.stop)
        guard = mock.patch("socket.socket.connect", side_effect=AssertionError("Сеть запрещена"))
        guard.start()
        self.addCleanup(guard.stop)
        self.default = self.home / ".config/asana/auth.json"
        self.default.parent.mkdir(parents=True)
        self.default.write_text(json.dumps({"token": " DEFAULT-PAT \n"}))
        self.explicit = self.home / "account.json"
        self.explicit.write_text(json.dumps({"token": " FILE-PAT \n"}))
        self.modules = {}
        for name in ("comments", "project", "blockers"):
            spec = importlib.util.spec_from_file_location("auth_test_" + name, SCRIPTS / f"asana-{name}.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.modules[name] = module

    def comments(self, *args):
        out, err = io.StringIO(), io.StringIO()
        stories = [
            {"created_at": f"2026-07-{day}T12:00:00.000Z", "created_by": {"name": "Автор"},
             "resource_subtype": kind, "text": f"event-{day}"}
            for day, kind in (("18", "comment_added"), ("20", "assigned"), ("21", "comment_added"))
        ]
        with mock.patch("sys.argv", ["asana-comments.py", *args]), \
                mock.patch.object(self.modules["comments"], "fetch_stories", return_value=stories) as fetch, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = self.modules["comments"].main()
            except SystemExit as exc:
                code = exc.code
                if not isinstance(code, (int, type(None))):
                    print(code, file=err)
                    code = 1
        return code or 0, out.getvalue(), err.getvalue(), fetch

    def test_comments_help_lists_auth(self):
        code, out, err, fetch = self.comments("--help")
        self.assertEqual(code, 0, err)
        self.assertIn("--auth", out)
        fetch.assert_not_called()

    def test_comments_auth_with_selection_flags(self):
        for flags, count in ((["--last", "1"], 1), (["--all", "--last", "0"], 2)):
            with self.subTest(flags=flags):
                code, out, err, fetch = self.comments(
                    "123", "--auth", str(self.explicit), "--system", "--since", "2026-07-20", *flags)
                self.assertEqual(code, 0, err)
                fetch.assert_called_once_with("123", "FILE-PAT")
                self.assertIn(f"показано {count} из 2", out.lower())
                self.assertNotIn("event-18", out)
                self.assertIn("event-21", out)
                self.assertEqual("event-20" in out, count == 2)

    def check_priority(self, auth, expected):
        for name, module in self.modules.items():
            with self.subTest(script=name):
                if name == "comments":
                    args = ["123"] + (["--auth", auth] if auth else [])
                    code, out, err, fetch = self.comments(*args)
                    self.assertEqual(code, 0, err)
                    fetch.assert_called_once_with("123", expected)
                else:
                    self.assertEqual(module.load_token(auth), expected)

    def test_explicit_tilde_file_beats_environment_and_default(self):
        self.check_priority("~/account.json", "FILE-PAT")

    def test_environment_beats_existing_default_and_is_trimmed(self):
        self.check_priority(None, "ENV-PAT")

    def test_default_when_environment_absent_or_blank(self):
        for value in (None, "", " \t\n"):
            with self.subTest(environment=value):
                if value is None:
                    os.environ.pop("ASANA_TOKEN", None)
                else:
                    os.environ["ASANA_TOKEN"] = value
                self.check_priority(None, "DEFAULT-PAT")

    def assert_auth_failure(self, *args):
        code, out, err, fetch = self.comments("123", *args)
        fetch.assert_not_called()
        self.assertEqual(code, 1, err)
        self.assertTrue(err.strip(), "Нужна диагностика в stderr")
        self.assertNotIn("Traceback", err)
        for token in ("FILE-PAT", "ENV-PAT", "DEFAULT-PAT", "MALFORMED-SECRET"):
            self.assertNotIn(token, out + err)
        return err

    def test_explicit_invalid_files_never_fall_back(self):
        for content in (None, '{"token": "MALFORMED-SECRET",', '{}', '{"token": null}',
                        '{"token": ""}', '{"token": "   "}'):
            with self.subTest(content=content):
                self.explicit.unlink(missing_ok=True)
                if content is not None:
                    self.explicit.write_text(content)
                self.assert_auth_failure("--auth", str(self.explicit))

    def test_explicit_unreadable_file_never_falls_back(self):
        # chmod недостаточен под root: моделируем EACCES на границе файлового IO.
        def deny(real_open):
            def wrapped(file, *args, **kwargs):
                if not isinstance(file, int) and Path(file) == self.explicit:
                    raise PermissionError("test auth permission denied")
                return real_open(file, *args, **kwargs)
            return wrapped
        with mock.patch.object(builtins, "open", deny(builtins.open)), \
                mock.patch.object(io, "open", deny(io.open)):
            self.assert_auth_failure("--auth", str(self.explicit))

    def test_no_sources_explains_auth_setup(self):
        os.environ.pop("ASANA_TOKEN", None)
        self.default.unlink()
        err = self.assert_auth_failure()
        self.assertIn("--auth", err)


if __name__ == "__main__":
    unittest.main()
