#!/usr/bin/env python3
"""canon-delta.py - детерминированная дельта-синхронизация канона (этап 8, часть a).

Заменяет LLM-оркестрацию /canon детерминированным дельта-скриптом поверх
immutable release-descriptor (canon.lock.json от build-lock.py). LLM - только
on-demand на разрешение конфликтов. Транзакционный (WAL) apply, per-project
модель состояния с расщеплением ownership (intent/state/ledger). См. дизайн
docs/design-2026-07-14-stage8-canon-sync.md (claude-control).

Расщепление состояния (§3):
- canon.intent.yaml (человек): project_type, track, skip_sync, local_only, overrides.
- canon.state.json  (этот скрипт, единственный писатель machine-полей):
  file_hashes{path:{sha,mode}}, membership, desired_release, applied_release,
  rollout_record, resolution_records.
- harvester-ledger (harvester): upstream_pending.

Этот файл - срез 1: утилиты, модель состояния, UNION-классификатор (§3 п.4),
команда classify (read-only анализ). WAL-apply / resolution / recovery - далее.

Запуск:
  canon-delta.py classify --lock canon.lock.json     # классификация путей (JSON)
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import os
import shutil
import stat
import sys
import time
import uuid
from pathlib import Path

CLI_VERSION = 1

# --- exit-коды (дизайн §3 п.6) ---
EXIT_OK = 0
EXIT_TRANSPORT = 1
EXIT_INCOMPAT = 2
EXIT_RECOVERY = 3
EXIT_CONFLICTS = 10

# --- классы (дизайн §3 п.4) ---
UP_TO_DATE = "up-to-date"
OUTDATED = "outdated"
LOCAL_EDIT = "local-edit"
RESOLVED_LOCAL = "resolved-local"
CONFLICT = "conflict"
UNTRACKED_COLLISION = "untracked-collision"
NEW = "new"
REMOVED_UPSTREAM = "removed-upstream"
RETIRED_FROM_SCOPE = "retired-from-scope"
MANAGED_BUT_EXCLUDED = "managed-but-excluded"
MISSING_LOCAL = "missing-local"

# классы, которые НИКОГДА не авто-применяются (§6 ownership)
NON_APPLY = {
    CONFLICT, LOCAL_EDIT, UNTRACKED_COLLISION, RESOLVED_LOCAL,
    REMOVED_UPSTREAM, RETIRED_FROM_SCOPE, MANAGED_BUT_EXCLUDED,
}
# классы, требующие эскалации человеку в дайджест (§5)
ESCALATE = {CONFLICT, UNTRACKED_COLLISION, REMOVED_UPSTREAM}

UNIVERSAL = "universal"


def die(msg: str, code: int = EXIT_INCOMPAT) -> None:
    sys.stderr.write(f"canon-delta: {msg}\n")
    sys.exit(code)


# =========================================================================
# Утилиты
# =========================================================================

def git_blob_sha(data: bytes) -> str:
    """git blob object SHA (тот же алгоритм, что git ls-tree/hash-object):
    sha1('blob <len>\\0' + data). Согласовано с descriptor.blob_sha из build-lock
    (git ls-tree) - иначе local vs upstream никогда не сравнить. Считается локально
    без вызова git."""
    h = hashlib.sha1()
    h.update(b"blob %d\x00" % len(data))
    h.update(data)
    return h.hexdigest()


def canonical_json(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _write_all(fd: int, data: bytes) -> None:
    """Полная запись буфера: os.write может записать МЕНЬШЕ (короткая запись на
    EINTR/сигнале) - дописываем до конца, иначе на диск ляжет усеченный файл, а
    вызывающий запишет ожидаемый sha в state (blocker)."""
    mv = memoryview(data)
    off = 0
    while off < len(data):
        n = os.write(fd, mv[off:])
        if n <= 0:  # zero-write на нестандартной ФС -> ошибка, не вечный цикл под flock
            raise OSError(f"нулевая запись в fd на off={off}/{len(data)}")
        off += n


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o644) -> None:
    """temp -> полная запись -> fchmod -> fsync -> rename + fsync каталога (§10).
    fchmod ДО fsync: режим durable вместе с байтами (после power-loss нет байт-без-+x).
    Полная запись через _write_all: короткая запись не даст усеченный файл."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp.{uuid.uuid4().hex}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        _write_all(fd, data)
        os.fchmod(fd, mode)  # точный режим (O_CREAT искажается umask) + durable через fsync ниже
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def git_mode_of(st: os.stat_result) -> str:
    """git-режим файла по POSIX stat: symlink=120000, owner-exec=100755, else 100644."""
    if stat.S_ISLNK(st.st_mode):
        return "120000"
    if st.st_mode & 0o100:  # owner execute bit (git смотрит на него)
        return "100755"
    return "100644"


def read_local(root: Path, rel: str) -> dict:
    """Локальное состояние файла проекта через lstat (НЕ следуя symlink, находка 7).

    Возвращает {exists, symlink, sha, mode}. symlink на месте канон-файла -> sha/mode
    считаются по самой ссылке (git-mode 120000), классификатор даст conflict.
    """
    p = root / rel
    try:
        st = p.lstat()
    except FileNotFoundError:
        return {"exists": False, "symlink": False, "sha": None, "mode": None}
    mode = git_mode_of(st)
    if stat.S_ISLNK(st.st_mode):
        # git хранит symlink как blob с содержимым = target-путь (не следуем)
        target = os.readlink(p)
        return {"exists": True, "symlink": True,
                "sha": git_blob_sha(target.encode("utf-8")), "mode": mode}
    data = p.read_bytes()
    return {"exists": True, "symlink": False, "sha": git_blob_sha(data), "mode": mode}


# =========================================================================
# Мини-YAML для intent (stdlib-only: подмножество - скаляры и списки строк)
# =========================================================================

def _strip_inline_comment(s: str) -> str:
    if " #" in s:
        s = s.split(" #", 1)[0]
    return s.strip()


def parse_intent_yaml(text: str) -> dict:
    """Мини-парсер canon.intent.yaml. Поддерживает:
      key: scalar
      key: [a, b, c]          # inline-список
      key:
        - a                   # блок-список
        - b
    Достаточно для intent (project_type/track/skip_sync/local_only/overrides).
    """
    result: dict = {}
    current_key: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            if current_key is None:
                die("intent.yaml: элемент списка вне ключа")
            item = _strip_inline_comment(stripped[2:])
            if item:
                if not isinstance(result.get(current_key), list):
                    result[current_key] = []
                result[current_key].append(item)
            continue
        if ":" in stripped:
            key, _, rest = stripped.partition(":")
            key = key.strip()
            rest = _strip_inline_comment(rest)
            current_key = key
            if rest == "":
                result[key] = []  # ожидаем блок-список ниже (или пусто)
            elif rest.startswith("[") and rest.endswith("]"):
                inner = rest[1:-1].strip()
                result[key] = [x.strip() for x in inner.split(",") if x.strip()] if inner else []
            else:
                result[key] = rest  # скаляр
            continue
    return result


def load_intent(path: Path) -> dict:
    if not path.exists():
        die(f"intent не найден: {path}")
    data = parse_intent_yaml(path.read_text(encoding="utf-8"))
    intent = {
        "project_type": data.get("project_type") or [],
        "track": data.get("track") or "stable",
        "skip_sync": data.get("skip_sync") or [],
        "local_only": data.get("local_only") or [],
        "overrides": data.get("overrides") or [],
    }
    if isinstance(intent["project_type"], str):
        intent["project_type"] = [intent["project_type"]]
    return intent


# =========================================================================
# Модель состояния (canon.state.json)
# =========================================================================

def empty_state() -> dict:
    return {
        "schema": 2,
        "file_hashes": {},        # path -> {sha, mode}
        "membership": {},         # path -> [секции]
        "desired_release": None,  # {commit_sha, manifest_digest, ...} или None
        "applied_release": None,
        "rollout_record": [],     # [commit_sha, ...] N последних (bound=3)
        "resolution_records": [], # keep-local: [{path, base_sha, local_sha, upstream_sha, release}]
        "decision_records": [],   # accept-upstream: [{path, outcome, release, upstream_sha}]
        "retirement_records": [], # [{path, release, upstream_sha_at_retire}] (T1 retire-lifecycle)
        "recovery_conflicts": [], # committed-recovery abort: [{path, release, reason}] (terminal-conflict, §10)
    }


def load_state(path: Path) -> dict:
    if not path.exists():
        return empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(f"state.json битый: {e}")
    base = empty_state()
    base.update(data)
    return base


def save_state(path: Path, state: dict) -> None:
    """Единственный писатель machine-полей state; atomic-rename (§3)."""
    atomic_write_text(path, json.dumps(state, sort_keys=True, ensure_ascii=True, indent=2) + "\n")


# =========================================================================
# Descriptor (canon.lock.json)
# =========================================================================

def load_descriptor(path: Path) -> dict:
    if not path.exists():
        die(f"lock не найден: {path}", EXIT_TRANSPORT)
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(f"lock не JSON: {e}", EXIT_INCOMPAT)
    # schema/min_cli fail-closed (§2, находка 18)
    sv = d.get("schema_version")
    if sv is None or sv > 1:
        die(f"lock schema_version={sv} несовместим (CLI поддерживает 1)", EXIT_INCOMPAT)
    mcv = d.get("min_cli_version", 1)
    if CLI_VERSION < mcv:
        die(f"lock требует min_cli_version={mcv}, у CLI {CLI_VERSION}", EXIT_INCOMPAT)
    return d


# =========================================================================
# UNION-классификатор (§3 п.4, §6) - ЧИСТАЯ функция
# =========================================================================

def compute_type_set_sections(project_type: list[str]) -> set[str]:
    """Секции, применимые проекту: universal всегда + типы проекта."""
    return {UNIVERSAL, *project_type}


def applicable_paths(descriptor: dict, sections: set[str], excluded: set[str]) -> set[str]:
    """Пути descriptor, чей membership пересекается с секциями проекта, минус
    intent-исключения (local_only/skip_sync/overrides; находка 5)."""
    membership = descriptor.get("membership", {})
    out = set()
    for path in descriptor.get("files", {}):
        secs = set(membership.get(path, []))
        if secs & sections and path not in excluded:
            out.add(path)
    return out


def _match_resolution(records: list[dict], path: str, local_sha: str, upstream_sha: str) -> bool:
    """resolved-local матчит, если есть record с тем же (path, local_sha, upstream_sha).
    Инвалидация (R6): при движении upstream_sha record не матчит -> путь всплывет
    как conflict, stale-record не подавляет неактуальное расхождение."""
    for r in records:
        if (r.get("path") == path and r.get("local_sha") == local_sha
                and r.get("upstream_sha") == upstream_sha):
            return True
    return False


def classify(intent: dict, state: dict, descriptor: dict, root: Path) -> list[dict]:
    """Классифицирует UNION = (applicable descriptor) ∪ (пути из state.file_hashes).

    Возвращает список {path, klass, base, local, upstream}. Чистая относительно
    диска через root; сеть/git не трогает.
    """
    sections = compute_type_set_sections(intent["project_type"])
    intent_excluded = set(intent["local_only"]) | set(intent["skip_sync"]) | set(intent["overrides"])
    applic = applicable_paths(descriptor, sections, intent_excluded)

    dfiles = descriptor.get("files", {})
    dmemb = descriptor.get("membership", {})
    fh = state.get("file_hashes", {})
    records = state.get("resolution_records", [])

    union = applic | set(fh.keys()) | intent_excluded
    results: list[dict] = []

    for path in sorted(union):
        base = fh.get(path)  # {sha,mode} или None
        up = dfiles.get(path)  # {blob_sha,mode} или None
        upstream = {"sha": up["blob_sha"], "mode": up["mode"]} if up else None
        in_descriptor = path in dfiles
        in_state = path in fh
        in_applicable = path in applic
        excluded_by_intent = path in intent_excluded
        in_type_set_raw = in_descriptor and bool(set(dmemb.get(path, [])) & sections)

        rec = {"path": path, "base": base, "upstream": upstream}

        if not in_applicable:
            # путь вне применимого scope: разграничение intent vs membership (§3 п.4, T1)
            if excluded_by_intent:
                # намеренное исключение: под учетом, upstream не заменяет, не retire
                rec["klass"] = MANAGED_BUT_EXCLUDED
            elif in_descriptor and not in_type_set_raw and in_state:
                # жив в descriptor, но не для типа проекта -> выпал по membership/project_type
                rec["klass"] = RETIRED_FROM_SCOPE
            elif not in_descriptor and in_state:
                # глобально исчез из канона -> эскалация, НЕ тихий GC (П1)
                rec["klass"] = REMOVED_UPSTREAM
            else:
                # путь не про нас (нет ни в state, ни в applicable) - пропускаем
                continue
            results.append(rec)
            continue

        # путь В применимом scope
        local = read_local(root, path)
        rec["local"] = {"sha": local["sha"], "mode": local["mode"], "exists": local["exists"]}

        if local["symlink"]:
            rec["klass"] = CONFLICT  # symlink на месте канон-файла (находка 7)
        elif not in_state:
            # новый для проекта
            rec["klass"] = UNTRACKED_COLLISION if local["exists"] else NEW
        elif not local["exists"]:
            rec["klass"] = MISSING_LOCAL
        else:
            local_pair = (local["sha"], local["mode"])
            base_pair = (base["sha"], base["mode"]) if base else None
            up_pair = (upstream["sha"], upstream["mode"]) if upstream else None
            if _match_resolution(records, path, local["sha"], upstream["sha"] if upstream else None):
                rec["klass"] = RESOLVED_LOCAL
            elif up_pair is not None and local_pair == up_pair:
                rec["klass"] = UP_TO_DATE
            elif base_pair is not None and local_pair == base_pair:
                # локально не трогали; upstream двинулся -> ff
                rec["klass"] = OUTDATED if local_pair != up_pair else UP_TO_DATE
            elif base_pair is not None and base_pair == up_pair:
                # локально меняли, upstream не двигался -> оставить
                rec["klass"] = LOCAL_EDIT
            else:
                # и локально меняли, и upstream двинулся
                rec["klass"] = CONFLICT
        results.append(rec)

    return results


# =========================================================================
# fast-path и планировщик действий (§3 п.1, release-wide gate §3)
# =========================================================================

# группировка классов по действию
APPLY_CLASSES = {OUTDATED, NEW, MISSING_LOCAL}   # материализовать upstream-байты
RETIRE_CLASSES = {RETIRED_FROM_SCOPE}            # снять из state.file_hashes
NOOP_CLASSES = {UP_TO_DATE, RESOLVED_LOCAL, LOCAL_EDIT, MANAGED_BUT_EXCLUDED}
# ESCALATE (conflict/untracked-collision/removed-upstream) блокирует release-wide gate


def local_matches_state(state: dict, root: Path) -> bool:
    """Все пути из file_hashes на диске совпадают (sha+mode), никто не missing.
    Обязательное локальное перехеширование fast-path (R7): канон не двигался
    != проект соответствует."""
    for path, base in state.get("file_hashes", {}).items():
        local = read_local(root, path)
        if not local["exists"] or local["symlink"]:
            return False
        if local["sha"] != base.get("sha") or local["mode"] != base.get("mode"):
            return False
    return True


def fast_path(state: dict, target_commit: str, root: Path) -> bool:
    """no-op fast-path: уже на target-ревизии И локальное дерево совпадает с base.
    target_commit - identity доставленного descriptor (от git-транспорта, ambient).
    Возвращает True если делать нечего (0 работы, 0 LLM)."""
    applied = state.get("applied_release")
    if not applied or applied.get("commit_sha") != target_commit:
        return False
    return local_matches_state(state, root)


def plan_actions(results: list[dict]) -> dict:
    """От классификации к плану: что применять, что эскалировать, что retire.

    release_ready (§3 release-wide gate): applied_release двигается к target ТОЛЬКО
    когда нет escalate-классов (conflict/untracked-collision/removed-upstream) -
    иначе релиз не считается полностью разрешённым.
    """
    apply, escalate, retire, noop = [], [], [], []
    for r in results:
        k = r["klass"]
        if k in APPLY_CLASSES:
            apply.append(r)
        elif k in ESCALATE:
            escalate.append(r)
        elif k in RETIRE_CLASSES:
            retire.append(r)
        else:
            noop.append(r)
    return {
        "apply": apply,
        "escalate": escalate,
        "retire": retire,
        "noop": noop,
        "release_ready": len(escalate) == 0,
    }


# =========================================================================
# Транзакционный WAL-apply (§6, §10) - stage + atomic rename, self-contained журнал
# =========================================================================

JOURNAL_NAME = ".canon-journal.json"
STAGE_DIR = ".canon-stage"
BAK_DIR = ".canon-bak"
LOCK_NAME = ".canon-lock"

PHASE_PREPARE = "prepare"
PHASE_COMMITTED = "committed"
SCOPE_PER_PATH = "per-path"   # фиксирует только file_hashes[path], applied_release НЕ двигать
SCOPE_RELEASE = "release"     # двигает applied_release + file_hashes + rollout атомарно

ACTION_CREATE = "create"      # NEW: файла нет ни в base, ни на диске
ACTION_MODIFY = "modify"      # OUTDATED: файл есть в base, ff к upstream
ACTION_RESTORE = "restore"    # MISSING_LOCAL: есть в base, пропал с диска

_SUPPORTED_MODES = ("100644", "100755")


class CrashSim(Exception):
    """Симуляция kill процесса в точке протокола (только для fault-тестов §10)."""


class RecoveryRequired(Exception):
    """Стейдж потреблен, blob-source недоступен -> нужна пересборка (exit 3)."""


def _noop_fault(_name: str) -> None:
    pass


# --- blob-source: материализация target-байт по blob_sha (§10) ---

class DictBlobSource:
    """Тестовый источник блобов {blob_sha: bytes}. Всегда сверяет sha."""

    def __init__(self, mapping: dict[str, bytes]) -> None:
        self._m = mapping

    def get(self, blob_sha: str) -> bytes:
        data = self._m.get(blob_sha)
        if data is None:
            raise RecoveryRequired(f"blob {blob_sha[:12]} нет в источнике")
        got = git_blob_sha(data)
        if got != blob_sha:
            raise ValueError(f"blob-source отдал {got[:12]} вместо {blob_sha[:12]}")
        return data


class GitMirrorBlobSource:
    """Материализация блоба из доверенного git-зеркала: git cat-file blob <sha>
    (НЕ <sha>:<path> - тот требует tree-контекста, §10). Сверяет sha локально."""

    def __init__(self, mirror: Path) -> None:
        self.mirror = Path(mirror)

    def get(self, blob_sha: str) -> bytes:
        import subprocess
        r = subprocess.run(
            ["git", "-C", str(self.mirror), "cat-file", "blob", blob_sha],
            capture_output=True,
        )
        if r.returncode != 0:
            raise RecoveryRequired(f"git cat-file blob {blob_sha[:12]}: {r.stderr.decode(errors='replace').strip()}")
        data = r.stdout
        got = git_blob_sha(data)
        if got != blob_sha:
            raise ValueError(f"зеркало отдало {got[:12]} вместо {blob_sha[:12]}")
        return data


# --- пути WAL (все под .claude/, тот же ФС - обязательно для atomic rename) ---

def journal_path(root: Path) -> Path:
    return root / ".claude" / JOURNAL_NAME


def _stage_rel(pass_id: str, rel: str) -> str:
    return f".claude/{STAGE_DIR}/{pass_id}/{rel}"


def _bak_rel(pass_id: str, rel: str) -> str:
    return f".claude/{BAK_DIR}/{pass_id}/{rel}"


def _new_pass_id() -> str:
    # uuid4-nonce, не time+pid: два вызова в одну секунду в одном процессе не
    # коллизируют namespace стейджа/бэкапа (находка 9)
    return f"{int(time.time())}-{uuid.uuid4().hex}"


def _mode_to_bits(mode: str) -> int:
    return 0o755 if mode == "100755" else 0o644


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except (FileNotFoundError, NotADirectoryError):
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def project_flock(root: Path):
    """Project-level advisory flock на весь проход (§6, находка 6). Один writer."""
    lp = root / ".claude" / LOCK_NAME
    lp.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lp, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --- журнал (atomic durability, §10) ---

def write_journal(root: Path, journal: dict) -> None:
    atomic_write_text(
        journal_path(root),
        json.dumps(journal, sort_keys=True, ensure_ascii=True, indent=2) + "\n",
    )


def read_journal(root: Path) -> dict | None:
    p = journal_path(root)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(f"WAL-журнал битый: {e}", EXIT_RECOVERY)


def clear_wal(root: Path, journal: dict) -> None:
    """clear-фаза: удалить журнал + стейдж + бэкапы этого pass-id (§6)."""
    pass_id = journal["header"]["pass_id"]
    for sub in (STAGE_DIR, BAK_DIR):
        d = root / ".claude" / sub / pass_id
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    jp = journal_path(root)
    if jp.exists():
        jp.unlink()
    _fsync_dir(root / ".claude")


# --- построение журнала (self-contained: файлы И machine-state, §10) ---

def build_journal(
    apply_items: list[dict], state: dict, target_release: dict,
    scope: str, pass_id: str, descriptor: dict, retire: list[str] | None = None,
) -> dict:
    fh = state.get("file_hashes", {})
    dfiles = descriptor["files"]
    dmemb = descriptor.get("membership", {})
    files: list[dict] = []
    for r in apply_items:
        path = r["path"]
        klass = r["klass"]
        up = dfiles[path]
        mode = up["mode"]
        if mode not in _SUPPORTED_MODES:
            die(f"unsupported mode {mode} для {path} (v1 без symlink-канона)", EXIT_INCOMPAT)
        if klass == NEW:
            action, base_sha, base_mode, backup = ACTION_CREATE, None, None, None
        elif klass == MISSING_LOCAL:
            b = fh.get(path) or {}
            action, base_sha, base_mode, backup = ACTION_RESTORE, b.get("sha"), b.get("mode"), None
        else:  # OUTDATED
            b = fh.get(path) or {}
            action = ACTION_MODIFY
            base_sha, base_mode = b.get("sha"), b.get("mode")
            backup = _bak_rel(pass_id, path)
        files.append({
            "path": path, "action": action,
            "base_sha": base_sha, "base_mode": base_mode,
            "target_sha": up["blob_sha"], "new_sha": up["blob_sha"], "mode": mode,
            "membership": dmemb.get(path, []),
            "staging_path": _stage_rel(pass_id, path),
            "backup_path": backup,
        })
    return {
        "header": {
            "release_identity": target_release["commit_sha"],
            "release": target_release,
            "phase": PHASE_PREPARE,
            "scope": scope,
            "pass_id": pass_id,
            "retire": list(retire or []),  # T1: снятие из state атомарно с commit (WAL-изовано)
        },
        "files": files,
    }


# --- prepare: материализация стейджа + бэкапов, затем flip -> committed (§6) ---

def materialize_staging(root: Path, entry: dict, blob_source) -> None:
    data = blob_source.get(entry["target_sha"])  # сверяет sha источника внутри
    stage = root / entry["staging_path"]
    atomic_write_bytes(stage, data, _mode_to_bits(entry["mode"]))
    # re-verify МАТЕРИАЛИЗОВАННОГО стейджа (ловит усечение/битую запись до commit, §10)
    if git_blob_sha(stage.read_bytes()) != entry["target_sha"]:
        raise RecoveryRequired(f"стейдж {entry['path']} != target_sha после записи")


def snapshot_backup(root: Path, entry: dict) -> None:
    if entry["backup_path"] is None:
        return
    data = (root / entry["path"]).read_bytes()
    atomic_write_bytes(root / entry["backup_path"], data)


def prepare(root: Path, journal: dict, blob_source, fault=_noop_fault) -> dict:
    """Материализует ВСЕ target-байты в стейдж + пре-имиджи в бэкап, fsync,
    затем ставит phase=committed. Журнал phase=prepare пишется ПЕРВЫМ - крэш
    до flip оставляет prepare (recovery откатывает, диск не тронут). Возвращает
    журнал с phase=committed."""
    write_journal(root, journal)  # phase=prepare, durable до материализации
    for e in journal["files"]:
        materialize_staging(root, e, blob_source)
        fault("staged")
        snapshot_backup(root, e)
    fault("pre-flip")
    committed = {**journal, "header": {**journal["header"], "phase": PHASE_COMMITTED}}
    write_journal(root, committed)  # atomic flip prepare->committed
    fault("post-flip")
    return committed


# --- commit: atomic rename стейджа на финал + state.json, под flock и CAS (§10) ---

def _cas_ok(entry: dict, local: dict) -> bool:
    """RE-VERIFY перед rename (§10): modify требует on-disk == base (sha+mode);
    create/restore требуют on-disk отсутствия. Расхождение -> abort пути (не clobber)."""
    if entry["action"] == ACTION_MODIFY:
        return (local["exists"] and not local["symlink"]
                and local["sha"] == entry["base_sha"] and local["mode"] == entry["base_mode"])
    return not local["exists"]


def _place_no_clobber(stage: Path, final: Path) -> bool:
    """Атомарно поставить create/restore-файл БЕЗ перезаписи. os.link падает, если
    final существует (POSIX-атомарно, no TOCTOU). False = файл появился в окне ->
    abort, НЕ clobber (находка 5). ФС без hardlink (EXDEV/EPERM) -> НЕ os.replace
    (тот бы затер чужой файл), а O_EXCL-создание (портируемый атомарный no-clobber)."""
    try:
        os.link(stage, final)
    except FileExistsError:
        return False
    except OSError:
        # ФС без hardlink stage->final (EXDEV/EPERM): пишем temp В КАТАЛОГЕ final
        # (тот же девайс), ПОЛНОСТЬЮ fsync-им, ТОЛЬКО ПОТОМ atomic os.link(temp,final).
        # final публикуется целиком (link атомарен), НИКОГДА не частичным (blocker O_EXCL).
        data = stage.read_bytes()
        mode = stage.stat().st_mode & 0o777
        tmp = final.parent / f".{final.name}.tmp.{uuid.uuid4().hex}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            _write_all(fd, data)
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp, final)  # атомарно; temp уже полностью на диске
        except FileExistsError:
            os.unlink(tmp)
            return False
        except OSError:
            os.unlink(tmp)  # даже temp+link не сработал -> НЕ публикуем частичное
            raise
        os.unlink(tmp)
        _fsync_dir(final.parent)
        stage.unlink()
        return True
    os.unlink(stage)
    return True


def _apply_one(root: Path, entry: dict, blob_source, fault=_noop_fault) -> str:
    """Идемпотентно применяет один файл: 'already' (уже == new по sha+mode), 'abort'
    (CAS-fail / чужой файл в окне), 'applied'. Стейдж потреблен -> re-fetch (§10).
    create/restore ставятся без перезаписи, modify - атомарной заменой после CAS."""
    local = read_local(root, entry["path"])
    if (local["exists"] and not local["symlink"]
            and local["sha"] == entry["new_sha"] and local["mode"] == entry["mode"]):
        return "already"
    if not _cas_ok(entry, local):
        return "abort"
    stage = root / entry["staging_path"]
    if not stage.exists():
        if blob_source is None:
            raise RecoveryRequired(entry["path"])
        atomic_write_bytes(stage, blob_source.get(entry["target_sha"]), _mode_to_bits(entry["mode"]))
    os.chmod(stage, _mode_to_bits(entry["mode"]))
    final = root / entry["path"]
    final.parent.mkdir(parents=True, exist_ok=True)
    fault("pre-rename:" + entry["path"])
    if entry["action"] == ACTION_MODIFY:
        os.replace(stage, final)  # атомарная замена существующего (CAS сверил base)
    elif not _place_no_clobber(stage, final):
        return "abort"  # create/restore: файл появился в окне, не затираем
    _fsync_dir(final.parent)
    fault("post-rename:" + entry["path"])
    return "applied"


def _finalize_state(state: dict, journal: dict, applied: list[dict], aborted: list[dict]) -> dict:
    """Строит новое state по scope журнала. per-path -> только file_hashes;
    release (и НЕТ aborted) -> + applied_release + rollout_record атомарно (§10, T2)."""
    st = copy.deepcopy(state)
    fh = st.setdefault("file_hashes", {})
    memb = st.setdefault("membership", {})
    for e in applied:
        fh[e["path"]] = {"sha": e["new_sha"], "mode": e["mode"]}
        memb[e["path"]] = e.get("membership", [])
    # retire T1: снять из file_hashes АТОМАРНО с commit (WAL-изовано), идемпотентно
    # под replay recovery (dedup по (path,release))
    rel_commit = (journal["header"].get("release") or {}).get("commit_sha")
    rec_list = st.setdefault("retirement_records", [])
    for path in journal["header"].get("retire", []):
        fh.pop(path, None)
        memb.pop(path, None)
        rec = {"path": path, "release": rel_commit}
        if rec not in rec_list:
            rec_list.append(rec)
    # decision-record per-path accept пишется АТОМАРНО с file_hashes (находка 8);
    # idempotent под replay recovery (не дублировать, находка 8-risk)
    decision = journal["header"].get("decision")
    if decision is not None and not aborted:
        dec_list = st.setdefault("decision_records", [])
        if decision not in dec_list:
            dec_list.append(decision)
    if journal["header"]["scope"] == SCOPE_RELEASE and not aborted:
        rel = journal["header"]["release"]
        st["applied_release"] = rel
        st["desired_release"] = rel
        rr = st.get("rollout_record", [])
        cs = rel["commit_sha"]
        if not rr or rr[-1] != cs:
            rr = (rr + [cs])[-3:]
        st["rollout_record"] = rr
    return st


def _commit_locked(root: Path, journal: dict, state: dict, state_path: Path,
                   blob_source=None, fault=_noop_fault) -> tuple[dict, list[dict]]:
    """commit-фаза БЕЗ взятия lock: rename каждого файла (CAS), затем финальный
    atomic-rename state.json. Вызывается под уже удерживаемым project_flock."""
    applied, aborted = [], []
    for e in journal["files"]:
        r = _apply_one(root, e, blob_source, fault)
        (applied if r in ("applied", "already") else aborted).append(e)
    fault("pre-state")
    new_state = _finalize_state(state, journal, applied, aborted)
    save_state(state_path, new_state)
    return new_state, aborted


def commit(root: Path, journal: dict, state: dict, state_path: Path,
           blob_source=None, fault=_noop_fault) -> tuple[dict, list[dict]]:
    """Публичный commit: берет project_flock и делегирует _commit_locked. В проходе
    apply_release/resolve lock держится снаружи (единый на весь проход)."""
    with project_flock(root):
        return _commit_locked(root, journal, state, state_path, blob_source, fault)


# --- recovery: терминальный (roll-forward ЛИБО roll-back), идемпотентный (§6, §10 T2) ---

def _restore_from_backup(root: Path, entry: dict) -> None:
    """Откат modified к пре-имиджу. Байты И mode приземляются атомарно одной
    atomic_write_bytes (mode на temp до rename) - нет частичного bytes-без-chmod
    состояния (находка 1)."""
    data = (root / entry["backup_path"]).read_bytes()
    atomic_write_bytes(root / entry["path"], data, _mode_to_bits(entry["base_mode"]))


def _recover_prepare(root: Path, journal: dict) -> list[str]:
    """phase=prepare: rename НЕ финализировался. identity сверяется парой (sha,mode)
    ВЕЗДЕ (находки 1,2). modify: ==base -> ничего; байты==new -> откат из backup (если
    backup есть, иначе no-touch, находка 6); иначе -> эскалация. create/restore:
    absent -> ничего; (sha,mode)==new -> МЫ создали, удалить; иначе -> чужой файл,
    НЕ трогать (находка 2)."""
    escalated: list[str] = []
    for e in journal["files"]:
        local = read_local(root, e["path"])
        if e["action"] == ACTION_MODIFY:
            if not local["exists"]:
                continue  # base пропал, rename не финализировался - ничего не портим
            if (local["sha"], local["mode"]) == (e["base_sha"], e["base_mode"]):
                continue  # чисто
            if (local["sha"], local["mode"]) == (e["new_sha"], e["mode"]):
                # наш post-image по ПОЛНОЙ identity (sha+mode) -> откат из backup
                bak = root / e["backup_path"] if e["backup_path"] else None
                if bak is None or not bak.exists():
                    escalated.append(e["path"])  # backup нет (crash до snapshot) -> no-touch
                    continue
                _restore_from_backup(root, e)
            else:
                escalated.append(e["path"])  # чужая правка / иной mode -> не трогаем
        else:  # create / restore
            if not local["exists"]:
                continue
            if (local["sha"], local["mode"]) == (e["new_sha"], e["mode"]):
                (root / e["path"]).unlink()  # это МЫ создали (sha+mode) -> удалить
            else:
                escalated.append(e["path"])  # чужой файл (иные байты/режим) -> не трогаем
    return escalated


def _recover_locked(root: Path, journal: dict, blob_source, state_path: Path) -> dict:
    """Терминализация под уже удерживаемым project_flock. journal прочитан снаружи."""
    phase = journal["header"]["phase"]
    if phase == PHASE_PREPARE:
        escalated = _recover_prepare(root, journal)
        clear_wal(root, journal)
        return {"status": "rolled-back", "escalated": escalated}
    # phase == committed: терминальный roll-forward
    state = load_state(state_path)
    applied, aborted = [], []
    for e in journal["files"]:
        r = _apply_one(root, e, blob_source)
        (applied if r in ("applied", "already") else aborted).append(e)
    new_state = _finalize_state(state, journal, applied, aborted)
    # aborted при committed = чужая правка после нашего rename: файл НЕ доигран.
    # Чтобы recovery был терминальным (§10) и НЕ терял намерение транзакции (в т.ч.
    # decision, который _finalize_state при abort не пишет) - фиксируем durable
    # recovery-conflict ДО очистки WAL. Так WAL чистится (терминально), но информация
    # не теряется: путь всплывет человеку через recovery_conflicts, не тихим drop.
    if aborted:
        rc = new_state.setdefault("recovery_conflicts", [])
        rel_id = journal["header"].get("release_identity")
        for e in aborted:
            item = {"path": e["path"], "release": rel_id, "reason": "committed-recovery-abort"}
            if item not in rc:
                rc.append(item)
    save_state(state_path, new_state)
    clear_wal(root, journal)
    return {
        "status": "rolled-forward" if not aborted else "rolled-forward-conflict",
        "applied": [e["path"] for e in applied],
        "aborted": [e["path"] for e in aborted],
    }


def recover(root: Path, blob_source=None, state_path: Path | None = None) -> dict:
    """Терминализует незавершенную транзакцию под project_flock. phase=prepare ->
    roll-back; phase=committed -> roll-forward (доиграть файлы + state.json по scope).
    ВСЕГДА доводит committed до конца, не оставляет частичный post-image со старым state."""
    if state_path is None:
        state_path = root / ".claude" / "canon.state.json"
    if read_journal(root) is None:
        return {"status": "clean"}
    with project_flock(root):
        journal = read_journal(root)  # перечитать под lock (мог измениться)
        if journal is None:
            return {"status": "clean"}
        return _recover_locked(root, journal, blob_source, state_path)


# --- оркестратор apply: classify -> reconcile -> desired -> WAL(files+retire) -> clear ---

def apply_release(root: Path, intent: dict, state: dict, descriptor: dict,
                  target_commit: str, blob_source, state_path: Path) -> dict:
    """Полный проход под ЕДИНЫМ project_flock (находки 3,4): терминализация хвоста WAL
    -> fast-path -> classify/plan -> reconcile up-to-date drift -> desired_release ->
    prepare -> commit(files+retire атомарно) -> clear. state перечитывается с диска под
    lock. Легальный писатель pin: desired_release (atomic) до apply, commit пишет
    applied_release; release-wide gate - applied двигается ТОЛЬКО при scope=release без
    aborted. retire WAL-изован (в journal.header), applied атомарно в commit (находка
    retire-not-in-WAL)."""
    target_release = {"commit_sha": target_commit, "manifest_digest": descriptor["manifest_digest"]}
    with project_flock(root):
        # 0) добить незавершенную транзакцию под этим же lock (нельзя писать новый WAL поверх живого)
        j = read_journal(root)
        if j is not None:
            _recover_locked(root, j, blob_source, state_path)
        loaded = load_state(state_path)
        if read_journal(root) is not None:
            raise RecoveryRequired("WAL не терминализован после recovery")

        # 1) fast-path: уже на target и локальное дерево совпадает -> 0 работы
        if fast_path(loaded, target_commit, root):
            return {"status": "up-to-date", "plan": None, "applied": [], "aborted": [],
                    "scope": None, "state": loaded}

        results = classify(intent, loaded, descriptor, root)
        plan = plan_actions(results)
        scope = SCOPE_RELEASE if plan["release_ready"] else SCOPE_PER_PATH
        retire_paths = [r["path"] for r in plan["retire"]]

        # рабочая копия: reconcile up-to-date drift + desired_release
        work = copy.deepcopy(loaded)
        fh = work.setdefault("file_hashes", {})
        memb = work.setdefault("membership", {})
        # up-to-date пути со stale base -> обновить file_hashes (НЕ файл): иначе fast-path
        # навсегда false, а release мог бы двинуться при stale base (находка 7, вектор 4)
        for r in results:
            if r["klass"] == UP_TO_DATE and r.get("upstream"):
                up = r["upstream"]
                pair = {"sha": up["sha"], "mode": up["mode"]}
                if fh.get(r["path"]) != pair:
                    fh[r["path"]] = pair
                    memb[r["path"]] = descriptor.get("membership", {}).get(r["path"], [])
        work["desired_release"] = target_release

        apply_set = plan["apply"]

        if not apply_set:
            # только сдвиг указателя / retire / reconcile (state-only, один atomic-save)
            header = {"scope": scope, "release": target_release, "retire": retire_paths}
            new_state = _finalize_state(work, {"header": header}, [], [])
            if new_state != loaded:
                save_state(state_path, new_state)
            return {"status": "no-file-work", "plan": plan, "applied": [], "aborted": [],
                    "scope": scope, "state": new_state}

        # persist desired+reconcile до WAL (легальный писатель pin)
        if work != loaded:
            save_state(state_path, work)

        pass_id = _new_pass_id()
        journal = build_journal(apply_set, work, target_release, scope, pass_id, descriptor, retire_paths)
        committed = prepare(root, journal, blob_source)
        new_state, aborted = _commit_locked(root, committed, work, state_path, blob_source)
        clear_wal(root, committed)
        aborted_paths = {e["path"] for e in aborted}
        return {
            "status": "applied" if scope == SCOPE_RELEASE and not aborted else "partial",
            "plan": plan,
            "applied": [e["path"] for e in journal["files"] if e["path"] not in aborted_paths],
            "aborted": list(aborted_paths),
            "scope": scope,
            "state": new_state,
        }


# =========================================================================
# Resolution-API (§3): 4 исхода конфликта, каждый ЗАМЫКАЕТ FSM
# =========================================================================

def _terminate_wal_locked(root: Path, blob_source, state_path: Path) -> dict:
    """Под удерживаемым lock: добить хвост WAL и вернуть свежее state. Гарантирует,
    что новый WAL не пишется поверх живого (находка 3 для resolve-пути)."""
    j = read_journal(root)
    if j is not None:
        _recover_locked(root, j, blob_source, state_path)
    state = load_state(state_path)
    if read_journal(root) is not None:
        raise RecoveryRequired("WAL не терминализован после recovery")
    return state


def resolve_keep_local(root: Path, descriptor: dict, path: str, target_commit: str | None,
                       state_path: Path, blob_source=None) -> dict:
    """keep-local: durable resolution_record {path, base, local, upstream}. Файл НЕ
    трогаем, base НЕ обновляем (иначе теряем факт расхождения). Классификатор при
    том же (upstream_sha, local_sha) выдаст resolved-local. R6: при движении upstream
    record перестанет матчить -> путь всплывет как новый conflict. Под flock +
    recover-first: не пишем state поверх живого WAL."""
    with project_flock(root):
        state = _terminate_wal_locked(root, blob_source, state_path)
        local = read_local(root, path)
        up = descriptor.get("files", {}).get(path)
        upstream_sha = up["blob_sha"] if up else None
        base = state.get("file_hashes", {}).get(path) or {}
        rec = {
            "path": path,
            "base_sha": base.get("sha"),
            "local_sha": local["sha"],
            "upstream_sha": upstream_sha,
            "release": target_commit,
        }
        st = copy.deepcopy(state)
        # dedup по пути: держим только актуальное решение
        st["resolution_records"] = [
            r for r in st.get("resolution_records", []) if r.get("path") != path
        ] + [rec]
        save_state(state_path, st)
        return st


def resolve_accept_upstream(root: Path, descriptor: dict, path: str,
                            target_commit: str, blob_source, state_path: Path) -> dict:
    """accept-upstream (per-path): материализовать upstream-байты пути через WAL
    scope=per-path -> обновляет ТОЛЬКО file_hashes[path] + decision-record (атомарно
    в commit, находка 8). applied_release НЕ двигается (§3, П3). CAS по ТЕКУЩЕМУ
    локальному sha (discard именно той правки, что видел человек); новая правка в
    окне -> abort. Под flock + recover-first (находка 3)."""
    up = descriptor.get("files", {}).get(path)
    if up is None:
        die(f"accept: путь {path} отсутствует в descriptor", EXIT_INCOMPAT)
    mode = up["mode"]
    if mode not in _SUPPORTED_MODES:
        die(f"accept: unsupported mode {mode} для {path}", EXIT_INCOMPAT)
    target_release = {"commit_sha": target_commit, "manifest_digest": descriptor["manifest_digest"]}
    with project_flock(root):
        state = _terminate_wal_locked(root, blob_source, state_path)
        local = read_local(root, path)
        pass_id = _new_pass_id()
        entry = {
            "path": path,
            "action": ACTION_MODIFY if local["exists"] else ACTION_CREATE,
            "base_sha": local["sha"] if local["exists"] else None,
            "base_mode": local["mode"] if local["exists"] else None,
            "target_sha": up["blob_sha"], "new_sha": up["blob_sha"], "mode": mode,
            "membership": descriptor.get("membership", {}).get(path, []),
            "staging_path": _stage_rel(pass_id, path),
            "backup_path": _bak_rel(pass_id, path) if local["exists"] else None,
        }
        decision = {"path": path, "outcome": "accept-upstream",
                    "release": target_commit, "upstream_sha": up["blob_sha"]}
        journal = {
            "header": {"release_identity": target_commit, "release": target_release,
                       "phase": PHASE_PREPARE, "scope": SCOPE_PER_PATH,
                       "pass_id": pass_id, "decision": decision},
            "files": [entry],
        }
        committed = prepare(root, journal, blob_source)
        new_state, aborted = _commit_locked(root, committed, state, state_path, blob_source)
        clear_wal(root, committed)
    return {"path": path, "aborted": [e["path"] for e in aborted], "state": new_state}


# =========================================================================
# CLI
# =========================================================================

def _default_root() -> Path:
    env = os.environ.get("CANON_PROJECT_ROOT")
    return Path(env).resolve() if env else Path(__file__).resolve().parent.parent


def cmd_classify(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve() if args.root else _default_root()
    intent = load_intent(Path(args.intent) if args.intent else root / ".claude" / "canon.intent.yaml")
    state = load_state(Path(args.state) if args.state else root / ".claude" / "canon.state.json")
    descriptor = load_descriptor(Path(args.lock))
    results = classify(intent, state, descriptor, root)
    summary: dict[str, int] = {}
    for r in results:
        summary[r["klass"]] = summary.get(r["klass"], 0) + 1
    out = {"summary": summary, "items": results}
    print(json.dumps(out, ensure_ascii=True, indent=2, sort_keys=True))
    has_conflicts = any(r["klass"] in ESCALATE for r in results)
    return EXIT_CONFLICTS if has_conflicts else EXIT_OK


def _load_triple(args: argparse.Namespace) -> tuple[Path, dict, dict, dict]:
    root = Path(args.root).resolve() if args.root else _default_root()
    intent = load_intent(Path(args.intent) if args.intent else root / ".claude" / "canon.intent.yaml")
    state = load_state(Path(args.state) if args.state else root / ".claude" / "canon.state.json")
    descriptor = load_descriptor(Path(args.lock))
    return root, intent, state, descriptor


def cmd_plan(args: argparse.Namespace) -> int:
    root, intent, state, descriptor = _load_triple(args)
    results = classify(intent, state, descriptor, root)
    plan = plan_actions(results)
    counts = {k: len(plan[k]) for k in ("apply", "escalate", "retire", "noop")}
    out = {
        "counts": counts,
        "release_ready": plan["release_ready"],
        "apply": [{"path": r["path"], "klass": r["klass"]} for r in plan["apply"]],
        "escalate": [{"path": r["path"], "klass": r["klass"]} for r in plan["escalate"]],
        "retire": [{"path": r["path"], "klass": r["klass"]} for r in plan["retire"]],
    }
    print(json.dumps(out, ensure_ascii=True, indent=2, sort_keys=True))
    return EXIT_CONFLICTS if plan["escalate"] else EXIT_OK


def cmd_recover(args: argparse.Namespace) -> int:
    """Терминализовать незавершенную WAL-транзакцию (roll-forward/roll-back)."""
    root = Path(args.root).resolve() if args.root else _default_root()
    state_path = Path(args.state) if args.state else root / ".claude" / "canon.state.json"
    blob_source = GitMirrorBlobSource(Path(args.mirror)) if args.mirror else None
    try:
        res = recover(root, blob_source=blob_source, state_path=state_path)
    except RecoveryRequired as e:
        sys.stderr.write(f"canon-delta: recovery требует blob-source: {e}\n")
        return EXIT_RECOVERY
    print(json.dumps(res, ensure_ascii=True, indent=2, sort_keys=True))
    return EXIT_OK


def cmd_sync(args: argparse.Namespace) -> int:
    """Полный проход под единым flock (recover хвоста WAL + fast-path + apply внутри
    apply_release, §10). Требует --mirror (git-зеркало канона) и --target (commit_sha
    доставленной ревизии descriptor)."""
    root, intent, state, descriptor = _load_triple(args)
    state_path = Path(args.state) if args.state else root / ".claude" / "canon.state.json"
    blob_source = GitMirrorBlobSource(Path(args.mirror))
    try:
        res = apply_release(root, intent, state, descriptor, args.target, blob_source, state_path)
    except RecoveryRequired as e:
        sys.stderr.write(f"canon-delta: recovery-required: {e}\n")
        return EXIT_RECOVERY

    if res["status"] == "up-to-date":
        print(json.dumps({"status": "up-to-date", "target": args.target}, ensure_ascii=True, indent=2))
        return EXIT_OK
    plan = res["plan"]
    out = {
        "status": res["status"],
        "applied": res["applied"],
        "aborted": res["aborted"],
        "escalate": [{"path": r["path"], "klass": r["klass"]} for r in plan["escalate"]],
        "retire": [r["path"] for r in plan["retire"]],
        "release_ready": plan["release_ready"],
    }
    print(json.dumps(out, ensure_ascii=True, indent=2, sort_keys=True))
    return EXIT_CONFLICTS if plan["escalate"] else EXIT_OK


def cmd_resolve(args: argparse.Namespace) -> int:
    """Замкнуть один conflict-путь: accept (взять upstream, per-path WAL) /
    keep-local (durable record, файл не трогаем) / skip (правка intent - владелец
    человек, дельта не пишет intent: печатаем нужную строку, не применяем)."""
    root, intent, state, descriptor = _load_triple(args)
    state_path = Path(args.state) if args.state else root / ".claude" / "canon.state.json"
    blob_source = GitMirrorBlobSource(Path(args.mirror)) if args.mirror else None

    if args.outcome == "skip":
        sys.stderr.write(
            "canon-delta: skip - владелец intent (человек). Допиши путь в "
            "canon.intent.yaml -> skip_sync (дельта intent не трогает, ownership):\n"
            f"  skip_sync:\n    - {args.path}\n"
        )
        return EXIT_OK

    try:
        if args.outcome == "keep-local":
            resolve_keep_local(root, descriptor, args.path, args.target, state_path, blob_source)
            print(json.dumps({"path": args.path, "outcome": "keep-local"}, ensure_ascii=True, indent=2))
            return EXIT_OK
        # accept-upstream
        if not args.mirror or not args.target:
            die("accept требует --mirror и --target", EXIT_INCOMPAT)
        res = resolve_accept_upstream(root, descriptor, args.path, args.target, blob_source, state_path)
    except RecoveryRequired as e:
        sys.stderr.write(f"canon-delta: resolve не смог (recovery-required): {e}\n")
        return EXIT_RECOVERY
    status = "aborted" if res["aborted"] else "accepted"
    print(json.dumps({"path": args.path, "outcome": "accept-upstream", "status": status},
                     ensure_ascii=True, indent=2))
    return EXIT_CONFLICTS if res["aborted"] else EXIT_OK


def main() -> int:
    p = argparse.ArgumentParser(description="Детерминированная дельта-синхронизация канона")
    p.add_argument("--root", help="корень проекта (дефолт: env CANON_PROJECT_ROOT или ../ от скрипта)")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("classify", help="классифицировать пути (read-only анализ)")
    c.add_argument("--lock", required=True, help="путь к canon.lock.json (descriptor)")
    c.add_argument("--intent", help="путь к canon.intent.yaml")
    c.add_argument("--state", help="путь к canon.state.json")
    c.set_defaults(func=cmd_classify)

    pl = sub.add_parser("plan", help="план действий (apply/escalate/retire) + release-gate")
    pl.add_argument("--lock", required=True, help="путь к canon.lock.json (descriptor)")
    pl.add_argument("--intent", help="путь к canon.intent.yaml")
    pl.add_argument("--state", help="путь к canon.state.json")
    pl.set_defaults(func=cmd_plan)

    sy = sub.add_parser("sync", help="применить релиз транзакционно (WAL) + retire")
    sy.add_argument("--lock", required=True, help="путь к canon.lock.json (descriptor)")
    sy.add_argument("--intent", help="путь к canon.intent.yaml")
    sy.add_argument("--state", help="путь к canon.state.json")
    sy.add_argument("--mirror", required=True, help="git-зеркало канона (источник блобов)")
    sy.add_argument("--target", required=True, help="commit_sha доставленной ревизии descriptor")
    sy.set_defaults(func=cmd_sync)

    rc = sub.add_parser("recover", help="терминализовать хвост WAL (roll-forward/back)")
    rc.add_argument("--state", help="путь к canon.state.json")
    rc.add_argument("--mirror", help="git-зеркало (для re-fetch при потребленном стейдже)")
    rc.set_defaults(func=cmd_recover)

    rs = sub.add_parser("resolve", help="замкнуть conflict-путь (accept/keep-local/skip)")
    rs.add_argument("--path", required=True, help="путь конфликтного файла")
    rs.add_argument("--outcome", required=True, choices=["accept", "keep-local", "skip"])
    rs.add_argument("--lock", required=True, help="путь к canon.lock.json (descriptor)")
    rs.add_argument("--intent", help="путь к canon.intent.yaml")
    rs.add_argument("--state", help="путь к canon.state.json")
    rs.add_argument("--mirror", help="git-зеркало канона (для accept)")
    rs.add_argument("--target", help="commit_sha ревизии descriptor (для accept/keep-local)")
    rs.set_defaults(func=cmd_resolve)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
