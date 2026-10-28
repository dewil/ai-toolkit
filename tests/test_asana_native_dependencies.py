"""Независимые SDD-тесты SPEC v1 + нормативного дополнения v2.

Запуск: python -m unittest discover -s tests -p test_asana_native_dependencies.py

Три ConfigCompatibilityTests проверяют существующий load_config:
legacy — positive control, explicit/invalid mode — ожидаемый baseline RED.

Отсутствие новых публичных портов дает отдельный structural skip.
Такой skip не означает готовность native-функциональности или GREEN.
Тесты здесь не запускались. Реальных запросов Asana нет.
"""

import copy
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit


TOKEN = "TEST_TOKEN_NEVER_SERIALIZE_7c841f"
SCOPE = {"account": "1", "workspace": "10", "project": "100"}
SCOPE_KEY = "1:10:100"
API_PREFIX = "https://app.asana.com/api/1.0"

MIXED_HTML = """<body>СВЯЗИ ЗАДАЧИ
ЭТА ЗАДАЧА ЖДЕТ - ее нельзя сделать, пока не закрыты:
- <a data-asana-gid="21"/> - исполнитель не назначен, открыта
СВЯЗАННЫЕ ЗАДАЧИ - откуда выросла эта работа:
- <a data-asana-gid="99"/>
- - -

<b>Ручной текст</b></body>"""

OLD_URL = (
    "https://files.example.test/p.png?"
    "asset=500&X-Amz-Signature=old"
)
NEW_URL = (
    "https://files.example.test/p.png?"
    "asset=500&X-Amz-Signature=new"
)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def empty_state():
    return {"schema_version": 1, "scopes": {}}


def owned_state(status="owned"):
    return {
        "schema_version": 1,
        "scopes": {SCOPE_KEY: {"edges": {"20>21": status}}},
    }


def cfg(chains=None):
    return {
        "project": "100",
        "chains": {"A": ["B"]} if chains is None else copy.deepcopy(chains),
        "mode": "native",
    }


def task(gid, name=None):
    return {
        "gid": str(gid),
        "name": name if name is not None else "Task " + str(gid),
        "html_notes": "<body>Ручной текст</body>",
        "projects": [{"gid": "100"}],
        "workspace": {"gid": "10"},
        "completed": False,
        "assignee": None,
    }


def load_subject():
    """Ищет исходный script внутри репозитория, не импортируя тестовую копию."""
    root = Path(__file__).resolve().parents[1]
    matches = []
    excluded = {
        ".git", ".venv", "venv", "__pycache__", "node_modules",
        ".tox", ".mypy_cache", ".pytest_cache",
    }
    for directory, children, files in os.walk(root):
        children[:] = sorted(name for name in children if name not in excluded)
        if "asana-blockers.py" in files:
            matches.append(Path(directory) / "asana-blockers.py")
    if len(matches) != 1:
        raise RuntimeError(
            "Expected exactly one asana-blockers.py under repository root; "
            "found %d" % len(matches)
        )
    name = "_asana_native_dependencies_subject"
    spec = importlib.util.spec_from_file_location(name, matches[0])
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load asana-blockers.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SUBJECT = load_subject()


class NativeAPI:
    """Синтетический HTTP-порт: envelope на каждый отдельный запрос.

    Дополнительные reachable tasks не включаются автоматически в board listing.
    Graph и dependents согласованы; POST изменяет graph.
    Неизвестный маршрут немедленно проваливает тест.
    """

    def __init__(self, subject):
        self.subject = subject
        self.tasks = {"20": task("20", "A"), "21": task("21", "B")}
        self.board = ["20", "21"]
        self.deps = {"20": set(), "21": set()}
        self.attachments = {}
        self.downloads = {}
        self.calls = []
        self.mutations = []
        self.overrides = {}
        self.failure = None
        self.after_mutation = None

    @staticmethod
    def envelope(data, next_page=None):
        return {
            "data": copy.deepcopy(data),
            "next_page": copy.deepcopy(next_page),
        }

    def add_task(self, gid, name=None, on_board=False):
        gid = str(gid)
        self.tasks[gid] = task(gid, name)
        self.deps.setdefault(gid, set())
        if on_board:
            self.board.append(gid)
        return self.tasks[gid]

    def set_edge(self, source, target):
        self.deps.setdefault(str(source), set()).add(str(target))

    def dependency_rows(self, gid):
        return [{"gid": value} for value in sorted(self.deps.get(gid, set()))]

    def dependent_rows(self, gid):
        return [
            {"gid": source}
            for source in sorted(self.deps)
            if gid in self.deps[source]
        ]

    def __call__(self, method, path, token, payload=None):
        if token != TOKEN:
            raise AssertionError("Unexpected token")
        if not isinstance(path, str) or not path.startswith("/"):
            raise AssertionError("native_request requires a relative API path")
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise AssertionError("Absolute or fragmented API path")
        method = method.upper()
        self.calls.append((method, path, copy.deepcopy(payload)))

        override = self.overrides.get((method, path))
        if override is not None:
            result = override() if callable(override) else override
            if isinstance(result, BaseException):
                raise result
            return copy.deepcopy(result)

        route = parsed.path
        if method == "GET":
            if route == "/users/me":
                return self.envelope({"gid": "1"})
            if route == "/projects/100":
                return self.envelope(
                    {"gid": "100", "workspace": {"gid": "10"}}
                )
            if route == "/projects/100/tasks":
                return self.envelope([self.tasks[g] for g in self.board])

            if route == "/attachments":
                parent = parse_qs(parsed.query).get("parent", [])
                if len(parent) != 1 or parent[0] not in self.tasks:
                    raise AssertionError("Invalid attachment parent")
                return self.envelope(
                    [{"gid": g} for g in self.attachments.get(parent[0], [])]
                )
            match = re.fullmatch(
                r"/tasks/([^/]+)(?:/(dependencies|dependents))?",
                route,
            )
            if match:
                gid, collection = match.groups()
                if gid not in self.tasks:
                    raise self.subject.NativeError("api_data")
                if collection == "dependencies":
                    return self.envelope(self.dependency_rows(gid))
                if collection == "dependents":
                    return self.envelope(self.dependent_rows(gid))
                return self.envelope(self.tasks[gid])

            match = re.fullmatch(r"/attachments/([^/]+)", route)
            if match:
                gid = match.group(1)
                if gid not in self.downloads:
                    raise AssertionError("Unknown attachment")
                return self.envelope(
                    {"gid": gid, "download_url": self.downloads[gid]}
                )

        if method == "POST":
            match = re.fullmatch(
                r"/tasks/([^/]+)/(addDependencies|removeDependencies)", route
            )
            if match:
                gid, operation = match.groups()
                if not isinstance(payload, dict) or set(payload) != {
                    "dependencies"
                }:
                    raise AssertionError("Unexpected dependency payload")
                targets = payload["dependencies"]
                if not isinstance(targets, list) or not targets:
                    raise AssertionError("Expected nonempty dependency list")
                if not all(isinstance(value, str) for value in targets):
                    raise AssertionError("Dependency gids must be strings")
                self.mutations.append((method, path, copy.deepcopy(payload)))
                if self.failure is not None:
                    raise self.subject.NativeError(self.failure)
                for target in targets:
                    if operation == "addDependencies":
                        self.set_edge(gid, target)
                    else:
                        self.deps.setdefault(gid, set()).discard(target)
                if self.after_mutation is not None:
                    self.after_mutation(method, path, payload)
                return self.envelope({})

        if method == "PUT":
            match = re.fullmatch(r"/tasks/([^/]+)", route)
            if match:
                if not isinstance(payload, dict) or set(payload) != {
                    "html_notes"
                }:
                    raise AssertionError(
                        "Cleanup may write only html_notes, never other fields"
                    )
                self.mutations.append((method, path, copy.deepcopy(payload)))
                if self.failure is not None:
                    raise self.subject.NativeError(self.failure)
                self.tasks[match.group(1)]["html_notes"] = payload["html_notes"]
                return self.envelope(self.tasks[match.group(1)])

        raise AssertionError("Unexpected native_request: %s %s" % (method, path))


class ConfigCompatibilityTests(unittest.TestCase):
    def read_config(self, value):
        with tempfile.TemporaryDirectory(prefix="asana-config-") as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            return SUBJECT.load_config(str(path))

    def test_legacy_positive_control_preserves_exact_shape(self):
        value = {"board": {"project": "100", "chains": {"A": ["B"]}}}
        self.assertEqual(self.read_config(value), value)

    def test_baseline_red_explicit_valid_modes_are_preserved(self):
        for mode in ("native", "auto", "text"):
            with self.subTest(mode=mode):
                value = {
                    "board": {
                        "project": "100",
                        "chains": {"A": ["B"]},
                        "mode": mode,
                    }
                }
                self.assertEqual(self.read_config(value), value)

    def test_baseline_red_invalid_mode_exits_before_api(self):
        value = {
            "board": {
                "project": "100",
                "chains": {"A": ["B"]},
                "mode": "wat",
            }
        }
        if hasattr(SUBJECT, "native_request"):
            with mock.patch.object(
                SUBJECT,
                "native_request",
                side_effect=AssertionError("Invalid mode reached API"),
            ):
                with self.assertRaises(SystemExit):
                    self.read_config(value)
        else:
            with self.assertRaises(SystemExit):
                self.read_config(value)


class NativeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        required = (
            "NativeError",
            "native_request",
            "build_native_plan",
            "apply_native_plan",
            "write_native_state",
        )
        missing = [name for name in required if not hasattr(SUBJECT, name)]
        if missing:
            raise unittest.SkipTest(
                "STRUCTURAL NOTREADY: absent public native API: "
                + ", ".join(missing)
                + "; config baseline tests remain independent"
            )
        for name in required:
            if not callable(getattr(SUBJECT, name)):
                raise AssertionError("Public API is not callable: " + name)
        if not issubclass(SUBJECT.NativeError, Exception):
            raise AssertionError("NativeError must be an exception class")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="asana-native-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.state_path = self.directory / "native-state.json"
        self.api = NativeAPI(SUBJECT)
        self.api_patch = mock.patch.object(
            SUBJECT, "native_request", side_effect=self.api
        )
        self.api_patch.start()
        self.addCleanup(self.api_patch.stop)
        self.saved_states = []
        self.write_count = 0
        self.fail_write_at = None
        self.seed(empty_state())

    def seed(self, value):
        self.state_path.write_bytes(canonical(value))
        self.state_path.chmod(0o600)

    def disk_state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def fake_writer(self, path, state):
        """Persistence double: ошибка происходит до изменения disk state."""
        self.write_count += 1
        if self.write_count == self.fail_write_at:
            raise OSError("synthetic persistence failure")
        target = Path(path)
        self.assertEqual(target.resolve(), self.state_path)
        self.saved_states.append(copy.deepcopy(state))
        target.write_bytes(canonical(state))
        target.chmod(0o600)

    def plan(self, configuration=None, state=None, cleanup=False):
        return SUBJECT.build_native_plan(
            "board",
            cfg() if configuration is None else configuration,
            TOKEN,
            self.disk_state() if state is None else state,
            cleanup_text=cleanup,
        )

    def apply(self, plan, expected=None, path=None):
        with mock.patch.object(
            SUBJECT, "write_native_state", side_effect=self.fake_writer
        ):
            return SUBJECT.apply_native_plan(
                plan,
                TOKEN,
                str(self.state_path if path is None else path),
                plan["digest"] if expected is None else expected,
            )

    def assert_native_error(self, callback, code=None):
        with self.assertRaises(SUBJECT.NativeError) as caught:
            callback()
        if code is not None:
            self.assertEqual(caught.exception.code, code)
        return caught.exception

    def assert_result(self, result, status, added=0, removed=0, cleaned=0):
        self.assertEqual(result["status"], status)
        for key, expected in (
            ("added", added), ("removed", removed), ("cleaned", cleaned)
        ):
            self.assertIs(type(result[key]), int)
            self.assertEqual(result[key], expected)

    def edge_status(self, source="20", target="21"):
        scope = self.disk_state()["scopes"].get(SCOPE_KEY, {})
        return scope.get("edges", {}).get(source + ">" + target)

    def install_attachment(self):
        self.api.attachments["20"] = ["500"]
        self.api.downloads["500"] = OLD_URL
        self.api.tasks["20"]["html_notes"] = MIXED_HTML.replace(
            "<b>Ручной текст</b>",
            '<b>Ручной текст</b><img src="%s" alt="схема"/>' % OLD_URL,
        )
        self.api.set_edge("20", "21")

    def test_minimal_plan_direction_digest_state_hash_and_no_writes(self):
        state = empty_state()
        original = copy.deepcopy(state)
        configuration = cfg()
        plan = self.plan(configuration, state)
        self.assertEqual(plan["schema_version"], 1)
        self.assertEqual(plan["scope"], SCOPE)
        self.assertEqual(plan["desired"], [["20", "21"]])
        self.assertEqual(plan["add"], [["20", "21"]])
        self.assertEqual(plan["remove"], [])
        self.assertEqual(plan["cleanup"], [])
        self.assertEqual(plan["label"], "board")
        self.assertEqual(plan["cfg"], configuration)
        self.assertIs(plan["cleanup_text"], False)
        self.assertRegex(plan["digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            plan["state_sha256"], hashlib.sha256(canonical(state)).hexdigest()
        )
        self.assertIn("snapshots", plan)
        self.assertEqual(state, original)
        self.assertEqual(self.api.mutations, [])
        self.assertEqual(self.plan(configuration, state)["digest"], plan["digest"])

    def test_manual_edges_are_preserved_and_never_adopted(self):
        self.api.set_edge("20", "21")
        state = empty_state()
        plan = self.plan(state=state)
        self.assertEqual(plan["add"], [])
        self.assertEqual(plan["remove"], [])
        self.assertEqual(state, empty_state())
        stale = self.plan(cfg({}), state)
        self.assertEqual(stale["desired"], [])
        self.assertEqual(stale["remove"], [])
        self.assertEqual(self.api.mutations, [])

    def test_only_stale_owned_edges_are_removed_and_empty_chains_are_valid(self):
        self.api.set_edge("20", "21")
        self.api.add_task("22", "Manual")
        self.api.set_edge("20", "22")
        plan = self.plan(cfg({}), owned_state())
        self.assertEqual(plan["desired"], [])
        self.assertEqual(plan["add"], [])
        self.assertEqual(plan["remove"], [["20", "21"]])
        self.assertNotIn(["20", "22"], plan["remove"])

    def test_pending_and_uncertain_presence_does_not_establish_ownership(self):
        self.api.set_edge("20", "21")
        for status in ("pending_add", "pending_remove", "uncertain"):
            with self.subTest(status=status):
                state = owned_state(status)
                before = copy.deepcopy(state)
                desired = self.plan(state=state)
                stale = self.plan(cfg({}), state)
                self.assertEqual(desired["add"], [])
                self.assertEqual(stale["remove"], [])
                self.assertEqual(state, before)
        self.assertEqual(self.api.mutations, [])

    def test_ambiguous_names_duplicates_self_edges_and_scope_are_rejected(self):
        def duplicate_name():
            self.api.add_task("22", "A", on_board=True)

        def wrong_project():
            self.api.tasks["21"]["projects"] = [{"gid": "999"}]

        def wrong_workspace():
            self.api.tasks["21"]["workspace"] = {"gid": "999"}

        cases = (
            ("ambiguous-name", duplicate_name, cfg()),
            ("duplicate-edge", lambda: None, cfg({"A": ["B", "B"]})),
            ("self-edge", lambda: None, cfg({"A": ["A"]})),
            ("project", wrong_project, cfg()),
            ("workspace", wrong_workspace, cfg()),
        )
        for name, prepare, configuration in cases:
            with self.subTest(case=name):
                self.api.__init__(SUBJECT)
                prepare()
                self.assert_native_error(lambda: self.plan(configuration))
                self.assertEqual(self.api.mutations, [])

    def test_cycles_include_paths_outside_configuration_and_existing_cycles(self):
        self.api.add_task("22", "External")
        self.api.set_edge("21", "22")
        self.api.set_edge("22", "20")
        self.assert_native_error(self.plan, "cycle")
        self.api.set_edge("20", "21")
        self.assert_native_error(lambda: self.plan(cfg({})), "cycle")
        self.assertEqual(self.api.mutations, [])

    def test_inaccessible_graph_and_more_than_1000_tasks_refuse(self):
        self.api.set_edge("21", "404")
        self.assert_native_error(self.plan)
        self.api.deps["21"].clear()
        previous = "21"
        for number in range(1000, 2000):
            gid = str(number)
            self.api.add_task(gid)
            self.api.set_edge(previous, gid)
            previous = gid
        self.assert_native_error(self.plan)
        self.assertEqual(self.api.mutations, [])

    def test_degree_limit_counts_manual_dependencies_and_dependents(self):
        for direction in ("dependencies", "dependents"):
            with self.subTest(direction=direction):
                self.api.__init__(SUBJECT)
                for number in range(1000, 1030):
                    gid = str(number)
                    self.api.add_task(gid)
                    if direction == "dependencies":
                        self.api.set_edge("20", gid)
                    else:
                        self.api.set_edge(gid, "20")
                self.assert_native_error(self.plan, "limit")
                self.assertEqual(self.api.mutations, [])

    def test_collection_pagination_uses_relative_single_request_port(self):
        original = self.api.__call__
        pages = []

        def paginated(method, path, token, payload=None):
            parsed = urlsplit(path)
            if method == "GET" and parsed.path == "/tasks/20/dependencies":
                pages.append(path)
                if parse_qs(parsed.query).get("offset") == ["second"]:
                    return self.api.envelope([{"gid": "21"}])
                return self.api.envelope(
                    [],
                    {
                        "uri": API_PREFIX
                        + "/tasks/20/dependencies?limit=100&offset=second"
                    },
                )
            return original(method, path, token, payload)

        with mock.patch.object(
            SUBJECT, "native_request", side_effect=paginated
        ):
            plan = self.plan()
        self.assertEqual(plan["add"], [])
        self.assertTrue(
            any(
                path == "/tasks/20/dependencies?limit=100&offset=second"
                for path in pages
            )
        )
        self.assertTrue(all(path.startswith("/") for path in pages))

    def test_pagination_rejects_host_path_and_repeated_cursor(self):
        bad_uris = (
            "https://evil.example/api/1.0/tasks/20/dependencies?offset=x",
            API_PREFIX + "/tasks/21/dependencies?offset=x",
            API_PREFIX + "/tasks/20/dependencies?offset=repeated",
        )
        original = self.api.__call__
        for uri in bad_uris:
            with self.subTest(uri=uri):
                def broken(method, path, token, payload=None):
                    if (
                        method == "GET"
                        and urlsplit(path).path == "/tasks/20/dependencies"
                    ):
                        return self.api.envelope([], {"uri": uri})
                    return original(method, path, token, payload)

                with mock.patch.object(
                    SUBJECT, "native_request", side_effect=broken
                ):
                    self.assert_native_error(self.plan)
        self.assertEqual(self.api.mutations, [])

    def test_apply_add_persists_intent_before_post_then_owned_after_readback(self):
        plan = self.plan()
        observations = []
        original = self.api.__call__

        def observe(method, path, token, payload=None):
            if method == "POST":
                observations.append(self.edge_status())
            return original(method, path, token, payload)

        with mock.patch.object(SUBJECT, "native_request", side_effect=observe):
            result = self.apply(plan)
        self.assert_result(result, "applied", added=1)
        self.assertEqual(observations, ["pending_add"])
        self.assertEqual(self.edge_status(), "owned")
        self.assertEqual(
            self.api.mutations,
            [("POST", "/tasks/20/addDependencies", {"dependencies": ["21"]})],
        )
        post_index = next(
            i for i, call in enumerate(self.api.calls) if call[0] == "POST"
        )
        later_routes = {
            urlsplit(path).path
            for method, path, _ in self.api.calls[post_index + 1:]
            if method == "GET"
        }
        self.assertIn("/tasks/20/dependencies", later_routes)
        self.assertIn("/tasks/21/dependents", later_routes)
        self.assertNotIn("20", self.api.deps["21"])

    def test_apply_remove_journals_before_post_and_preserves_manual_edge(self):
        self.seed(owned_state())
        self.api.set_edge("20", "21")
        self.api.add_task("22", "Manual")
        self.api.set_edge("20", "22")
        plan = self.plan(cfg({}))
        observations = []
        original = self.api.__call__

        def observe(method, path, token, payload=None):
            if method == "POST":
                observations.append(self.edge_status())
            return original(method, path, token, payload)

        with mock.patch.object(SUBJECT, "native_request", side_effect=observe):
            result = self.apply(plan)
        self.assert_result(result, "applied", removed=1)
        self.assertEqual(observations, ["pending_remove"])
        self.assertIsNone(self.edge_status())
        self.assertEqual(self.api.deps["20"], {"22"})
        self.assertEqual(
            self.api.mutations,
            [("POST", "/tasks/20/removeDependencies",
              {"dependencies": ["21"]})],
        )

    def test_apply_unchanged_manual_desired_remains_unowned(self):
        self.api.set_edge("20", "21")
        result = self.apply(self.plan())
        self.assert_result(result, "unchanged")
        self.assertIsNone(self.edge_status())
        self.assertEqual(self.api.mutations, [])

    def test_failed_intent_write_prevents_any_http_mutation(self):
        plan = self.plan()
        self.fail_write_at = 1
        self.assert_native_error(lambda: self.apply(plan))
        self.assertEqual(self.disk_state(), empty_state())
        self.assertEqual(self.api.mutations, [])

    def test_failed_post_success_state_write_leaves_pending_and_raises(self):
        plan = self.plan()
        self.fail_write_at = 2
        self.assert_native_error(lambda: self.apply(plan))
        self.assertEqual(self.edge_status(), "pending_add")
        self.assertIn("21", self.api.deps["20"])
        self.assertEqual(len(self.api.mutations), 1)
        retry = self.plan()
        self.assertEqual(retry["add"], [])
        stale = self.plan(cfg({}))
        self.assertEqual(stale["remove"], [])
        self.assertEqual(self.edge_status(), "pending_add")

    def test_mutation_errors_are_not_retried_or_fallback_text_writes(self):
        for code in (402, 401, 403, 429, 500, "timeout", "network"):
            with self.subTest(code=code):
                self.api.__init__(SUBJECT)
                self.seed(empty_state())
                self.saved_states.clear()
                self.write_count = 0
                plan = self.plan()
                self.api.failure = code
                error = self.assert_native_error(
                    lambda: self.apply(plan), code
                )
                self.assertEqual(len(self.api.mutations), 1)
                self.assertEqual(self.api.mutations[0][0], "POST")
                self.assertIn(
                    self.edge_status(), ("pending_add", "uncertain")
                )
                self.assertNotIn(TOKEN, str(error))
                self.assertNotIn("<", str(error))
                self.assertNotIn("http", str(error).lower())
                stale = self.plan(cfg({}))
                self.assertEqual(stale["remove"], [])

    def test_graph_drift_after_preview_refuses_before_mutation(self):
        plan = self.plan()
        self.api.add_task("22", "External")
        self.api.set_edge("21", "22")
        self.assert_native_error(lambda: self.apply(plan), "drift")
        self.assertEqual(self.api.mutations, [])
        self.assertEqual(self.disk_state(), empty_state())

    def test_partial_progress_is_journaled_and_next_step_rechecks_graph(self):
        self.api.add_task("22", "C", on_board=True)
        plan = self.plan(cfg({"A": ["B"], "B": ["C"]}))
        original = self.api.__call__
        injected = False

        def drift_before_second_step(method, path, token, payload=None):
            nonlocal injected
            if (
                len(self.api.mutations) == 1
                and self.edge_status("20", "21") == "owned"
                and method == "GET"
                and not injected
            ):
                self.api.add_task("23", "External")
                self.api.set_edge("22", "23")
                injected = True
            return original(method, path, token, payload)

        with mock.patch.object(
            SUBJECT, "native_request", side_effect=drift_before_second_step
        ):
            self.assert_native_error(lambda: self.apply(plan), "drift")
        self.assertTrue(injected)
        self.assertEqual(len(self.api.mutations), 1)
        self.assertEqual(self.edge_status("20", "21"), "owned")
        self.assertIn("21", self.api.deps["20"])
        self.assertNotIn("22", self.api.deps["21"])
        self.assertFalse(
            any("removeDependencies" in path
                for _, path, _ in self.api.mutations)
        )

    def test_digest_and_strict_state_validation_precede_api_writes(self):
        plan = self.plan()
        wrong_digest = "0" * 64 if plan["digest"] != "0" * 64 else "1" * 64
        self.assert_native_error(
            lambda: self.apply(plan, expected=wrong_digest)
        )
        self.assertEqual(self.api.mutations, [])

        tampered = copy.deepcopy(plan)
        tampered["desired"] = []
        self.assert_native_error(lambda: self.apply(tampered))
        self.assertEqual(self.api.mutations, [])

        invalid_states = (
            {"schema_version": 2, "scopes": {}},
            {"schema_version": 1, "scopes": []},
            {"schema_version": 1, "scopes": {SCOPE_KEY: {"edges": []}}},
            owned_state("unknown"),
        )
        for invalid in invalid_states:
            with self.subTest(state=invalid):
                self.seed(invalid)
                self.assert_native_error(lambda: self.apply(plan))
                self.assertEqual(self.api.mutations, [])
        self.state_path.write_text("{broken-json", encoding="utf-8")
        self.assert_native_error(lambda: self.apply(plan))
        self.assertEqual(self.api.mutations, [])

        self.seed(owned_state("uncertain"))
        self.assert_native_error(lambda: self.apply(plan), "drift")
        self.assertEqual(self.api.mutations, [])

    def test_state_path_refusal_and_real_writer_private_permissions(self):
        plan = self.plan()
        relative = Path("asana-relative-state.json")
        missing_parent = self.directory / "missing" / "state.json"
        link = self.directory / "linked-state.json"
        link.symlink_to(self.state_path)
        parent_link = self.directory / "linked-parent"
        parent_link.symlink_to(self.directory, target_is_directory=True)
        for invalid in (
            relative, missing_parent, link, parent_link / "native-state.json"
        ):
            with self.subTest(path=str(invalid)):
                self.assert_native_error(
                    lambda: self.apply(plan, path=invalid), "path"
                )
                self.assertEqual(self.api.mutations, [])

        output = self.directory / "writer-state.json"
        output.write_text("old", encoding="utf-8")
        output.chmod(0o644)
        SUBJECT.write_native_state(str(output), owned_state())
        self.assertEqual(
            json.loads(output.read_text(encoding="utf-8")), owned_state()
        )
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_exclusive_stable_neighbor_lock_refuses_and_is_not_unlinked(self):
        plan = self.plan()
        lock_path = Path(str(self.state_path) + ".lock")
        with lock_path.open("a+b") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            inode = lock_path.stat().st_ino
            self.assert_native_error(lambda: self.apply(plan), "lock_busy")
            self.assertEqual(self.api.mutations, [])
            self.assertEqual(lock_path.stat().st_ino, inode)
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        self.assert_result(self.apply(plan), "applied", added=1)
        self.assertTrue(lock_path.exists())
        self.assertEqual(lock_path.stat().st_ino, inode)

    def test_cleanup_preserves_related_manual_html_and_manual_ownership(self):
        self.api.tasks["20"]["html_notes"] = MIXED_HTML
        self.api.set_edge("20", "21")
        self.assertEqual(self.plan()["cleanup"], [])
        plan = self.plan(cleanup=True)
        self.assertEqual(plan["cleanup"], ["20"])
        self.assertEqual(plan["desired"], [["20", "21"]])
        result = self.apply(plan)
        self.assert_result(result, "applied", cleaned=1)
        html = self.api.tasks["20"]["html_notes"]
        self.assertNotIn("ЭТА ЗАДАЧА ЖДЕТ", html)
        self.assertIn(
            'СВЯЗАННЫЕ ЗАДАЧИ - откуда выросла эта работа:\n'
            '- <a data-asana-gid="99"/>',
            html,
        )
        self.assertIn("<b>Ручной текст</b>", html)
        self.assertIsNone(self.edge_status())
        self.assertEqual(self.api.deps["20"], {"21"})
        self.assertFalse(any(method == "POST"
                             for method, _, _ in self.api.mutations))

        put_index = next(
            i for i, call in enumerate(self.api.calls) if call[0] == "PUT"
        )
        earlier_routes = {
            urlsplit(path).path
            for method, path, _ in self.api.calls[:put_index]
            if method == "GET"
        }
        self.assertIn("/tasks/20/dependencies", earlier_routes)
        self.assertIn("/tasks/21/dependents", earlier_routes)

    def test_cleanup_ambiguous_mentions_and_unknown_sections_are_retained(self):
        self.api.set_edge("20", "21")
        variants = (
            MIXED_HTML.replace(
                "исполнитель не назначен, открыта",
                "исполнитель не назначен, открыта; моя ручная приписка",
            ),
            MIXED_HTML.replace(
                "СВЯЗАННЫЕ ЗАДАЧИ - откуда выросла эта работа:",
                "НЕИЗВЕСТНАЯ СЕКЦИЯ:",
            ),
            MIXED_HTML.replace(
                'data-asana-gid="21"', 'data-asana-gid="22"'
            ),
        )
        for html in variants:
            with self.subTest(html=html):
                self.api.tasks["20"]["html_notes"] = html
                plan = self.plan(cleanup=True)
                self.assertNotIn("20", plan["cleanup"])
                self.apply(plan)
                self.assertEqual(self.api.tasks["20"]["html_notes"], html)
        self.assertEqual(self.api.mutations, [])

    def test_cleanup_manual_text_attributes_and_task_metadata_drift_refuse(self):
        self.api.set_edge("20", "21")
        self.api.tasks["20"]["html_notes"] = MIXED_HTML
        original = copy.deepcopy(self.api.tasks)
        for change in ("manual-text", "attribute", "name", "project", "workspace"):
            with self.subTest(change=change):
                self.api.tasks = copy.deepcopy(original)
                plan = self.plan(cleanup=True)
                current = self.api.tasks["20"]
                if change == "manual-text":
                    current["html_notes"] += "\nРучное изменение"
                elif change == "attribute":
                    current["html_notes"] = current["html_notes"].replace(
                        "<b>", '<b class="manual">'
                    )
                elif change == "name":
                    current["name"] = "Renamed"
                elif change == "project":
                    current["projects"] = [{"gid": "999"}]
                else:
                    current["workspace"] = {"gid": "999"}
                self.assert_native_error(lambda: self.apply(plan))
                self.assertEqual(self.api.mutations, [])

    def test_attachment_signature_value_rotation_uses_fresh_html(self):
        self.install_attachment()
        plan = self.plan(cleanup=True)
        self.api.downloads["500"] = NEW_URL
        self.api.tasks["20"]["html_notes"] = (
            self.api.tasks["20"]["html_notes"].replace(OLD_URL, NEW_URL)
        )
        fresh_plan = self.plan(cleanup=True)
        self.assertEqual(fresh_plan["digest"], plan["digest"])
        result = self.apply(plan)
        self.assert_result(result, "applied", cleaned=1)
        puts = [
            payload for method, _, payload in self.api.mutations
            if method == "PUT"
        ]
        self.assertEqual(len(puts), 1)
        self.assertIn(NEW_URL, puts[0]["html_notes"])
        self.assertNotIn(OLD_URL, puts[0]["html_notes"])
        self.assertIn('alt="схема"', puts[0]["html_notes"])
        self.assertIn("<b>Ручной текст</b>", puts[0]["html_notes"])

    def test_attachment_normalization_is_narrow_and_identity_is_preserved(self):
        changes = (
            "asset", "path", "host", "attachment-gid", "alt", "manual-text",
            "signing-key-added", "signing-key-removed", "unknown-signer",
            "unconfirmed-url", "ambiguous-gid", "unsigned-url",
            "ordinary-query-order",
        )
        for change in changes:
            with self.subTest(change=change):
                self.api.__init__(SUBJECT)
                self.install_attachment()
                self.seed(empty_state())

                if change == "unknown-signer":
                    old = (
                        "https://files.example.test/p.png?"
                        "asset=500&Unknown-Signature=old"
                    )
                    new = old.replace("=old", "=new")
                elif change == "unsigned-url":
                    old = "https://files.example.test/p.png?asset=500&v=old"
                    new = old.replace("=old", "=new")
                elif change == "ordinary-query-order":
                    old = (
                        "https://files.example.test/p.png?"
                        "asset=500&kind=image&X-Amz-Signature=old"
                    )
                    new = (
                        "https://files.example.test/p.png?"
                        "kind=image&asset=500&X-Amz-Signature=new"
                    )
                else:
                    old, new = OLD_URL, NEW_URL

                self.api.downloads["500"] = old
                self.api.tasks["20"]["html_notes"] = MIXED_HTML.replace(
                    "<b>Ручной текст</b>",
                    '<b>Ручной текст</b><img src="%s" alt="схема"/>' % old,
                )
                if change == "unconfirmed-url":
                    self.api.attachments["20"] = []
                if change == "signing-key-removed":
                    old += "&X-Amz-Security-Token=old-token"
                    self.api.downloads["500"] = old
                    self.api.tasks["20"]["html_notes"] = (
                        self.api.tasks["20"]["html_notes"].replace(OLD_URL, old)
                    )

                plan = self.plan(cleanup=True)
                if change == "asset":
                    new = new.replace("asset=500", "asset=501")
                elif change == "path":
                    new = new.replace("/p.png", "/other.png")
                elif change == "host":
                    new = new.replace("files.example.test", "other.example.test")
                elif change == "signing-key-added":
                    new += "&X-Amz-Security-Token=new-token"
                elif change == "attachment-gid":
                    self.api.attachments["20"] = ["501"]
                    self.api.downloads["501"] = new
                elif change == "ambiguous-gid":
                    self.api.attachments["20"] = ["500", "501"]
                    self.api.downloads["501"] = new

                self.api.downloads["500"] = new
                self.api.tasks["20"]["html_notes"] = (
                    self.api.tasks["20"]["html_notes"].replace(old, new)
                )
                if change == "alt":
                    self.api.tasks["20"]["html_notes"] = (
                        self.api.tasks["20"]["html_notes"].replace(
                            'alt="схема"', 'alt="ручная схема"'
                        )
                    )
                elif change == "manual-text":
                    self.api.tasks["20"]["html_notes"] += "Ручная приписка"
                self.assert_native_error(lambda: self.apply(plan))
                self.assertEqual(self.api.mutations, [])

    def test_plans_journal_and_errors_do_not_disclose_html_urls_or_token(self):
        self.install_attachment()
        plan = self.plan(cleanup=True)
        serialized = json.dumps(plan, ensure_ascii=False)
        for forbidden in (
            TOKEN, OLD_URL, "X-Amz-Signature=old", "<body>", "<img ",
            "<b>Ручной текст</b>",
        ):
            self.assertNotIn(forbidden, serialized)

        self.assert_result(self.apply(plan), "applied", cleaned=1)
        journal = self.state_path.read_text(encoding="utf-8")
        for forbidden in (TOKEN, OLD_URL, NEW_URL, "<body>", "<img "):
            self.assertNotIn(forbidden, journal)

        self.api.__init__(SUBJECT)
        self.seed(empty_state())
        plan = self.plan()
        self.api.failure = 402
        error = self.assert_native_error(lambda: self.apply(plan), 402)
        self.assertLess(len(str(error)), 200)
        for forbidden in (TOKEN, "<", "https://", "http://"):
            self.assertNotIn(forbidden, str(error))
        for saved in self.saved_states:
            dumped = json.dumps(saved, ensure_ascii=False)
            for forbidden in (TOKEN, OLD_URL, NEW_URL, "<body>"):
                self.assertNotIn(forbidden, dumped)


if __name__ == "__main__":
    unittest.main()
