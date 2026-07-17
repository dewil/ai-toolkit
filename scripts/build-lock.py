#!/usr/bin/env python3
"""build-lock.py - генератор release-descriptor canon.lock.json (этап 8, часть a).

Издаёт immutable release-descriptor ревизии канона для детерминированной
дельта-синхронизации (canon-delta.py). Запускается в CI на аннотированном теге
canon-vN (identity ревизии = git commit_sha, СЮДА не пишется - его сообщает
git-транспорт как объект-identity). Читает manifest.yaml, снимает blob_sha и
mode КАЖДОГО файла канона из git-tree (не с рабочего дерева - воспроизводимость),
считает membership (в каких секциях-типах путь) и manifest_digest.

Descriptor: {schema_version, manifest_digest, files{path:{blob_sha,mode}},
membership{path:[секции]}, min_cli_version, plugin_source}. manifest_digest -
оптимизация fast-path (равный состав -> равный digest), НЕ release-identity;
identity держит commit_sha (см. дизайн §2). rev/built - волатильные, в digest
не входят и в файл не пишутся.

Запуск:
  scripts/build-lock.py                         # descriptor от HEAD -> canon.lock.json
  scripts/build-lock.py --ref canon-v3          # от тега
  scripts/build-lock.py --check canon.lock.json # server-side gate: сверить lock с деревом ref (R1)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

SCHEMA_VERSION = 1
DEFAULT_MIN_CLI_VERSION = 1

# порядок секций фиксирован (как в manifest.yaml)
SECTIONS = ["universal", "coding", "documentation", "wiki", "management", "education"]

# корень toolkit-репо: env CANON_REPO_ROOT (CI/тест) переопределяет дефолт от __file__
_env_root = os.environ.get("CANON_REPO_ROOT")
REPO_ROOT = Path(_env_root).resolve() if _env_root else Path(__file__).resolve().parent.parent


def die(msg: str, code: int = 2) -> None:
    sys.stderr.write(f"build-lock: {msg}\n")
    sys.exit(code)


def parse_manifest(text: str) -> dict[str, list[str]]:
    """Мини-парсер manifest.yaml (stdlib-only, без PyYAML).

    Формат: строки '<section>:' открывают секцию, строки '  - <path> # коммент'
    добавляют путь. Инлайн-комментарии и пустые строки/комменты игнорируются.
    Возвращает {section: [paths]} в порядке появления.
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # элемент списка: начинается с '-' после отступа
        if stripped.startswith("- "):
            if current is None:
                die("manifest: элемент списка вне секции")
            item = stripped[2:].strip()
            # обрезать инлайн-комментарий (' # ...'), но не '#' внутри пути (их нет)
            if " #" in item:
                item = item.split(" #", 1)[0].strip()
            if item:
                sections[current].append(item)
            continue
        # заголовок секции: 'name:' без отступа
        if line == stripped and stripped.endswith(":") and " " not in stripped[:-1]:
            name = stripped[:-1]
            current = name
            sections.setdefault(name, [])
            continue
        # прочее (шапка-докблок и т.п.) - пропускаем
    return sections


def git_ls_tree(ref: str) -> dict[str, tuple[str, str]]:
    """{path: (mode, blob_sha)} по всем blob'ам дерева ref. mode - git-режим
    (100644/100755/120000). Читает из git-объектов, не с рабочего дерева."""
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-tree", "-r", ref],
            capture_output=True, text=True, check=True,
        ).stdout
    except FileNotFoundError:
        die("git не найден в PATH")
    except subprocess.CalledProcessError as e:
        die(f"git ls-tree {ref} упал: {e.stderr.strip()}", 2)
    entries: dict[str, tuple[str, str]] = {}
    for line in out.splitlines():
        # формат: '<mode> <type> <object>\t<path>'
        if "\t" not in line:
            continue
        meta, path = line.split("\t", 1)
        parts = meta.split()
        if len(parts) != 3:
            continue
        mode, obj_type, obj_sha = parts
        if obj_type != "blob":
            continue
        entries[path] = (mode, obj_sha)
    return entries


def resolve_commit(ref: str) -> str:
    """Полный commit_sha, на который указывает ref (тег -> коммит)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", f"{ref}^{{commit}}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as e:
        die(f"git rev-parse {ref} упал: {e.stderr.strip()}", 2)
    return out


def expand_paths(
    manifest_paths: dict[str, set[str]], tree: dict[str, tuple[str, str]]
) -> tuple[dict[str, dict[str, str]], dict[str, list[str]]]:
    """Раскрывает manifest-пути в реальные файлы дерева.

    - обычный файл (rules/agents/scripts/commands/x): один путь.
    - skills/<x>/... : скилл = ПАПКА, раскрывается во ВСЕ файлы skills/<x>/ из tree
      (скилл может нести не только SKILL.md). Секции наследуются от manifest-записи.

    Возвращает (files{path:{blob_sha,mode}}, membership{path:[sorted секции]}).
    """
    files: dict[str, dict[str, str]] = {}
    membership: dict[str, set[str]] = {}

    def add(real_path: str, sections: set[str]) -> None:
        # produce-сторона того же инварианта, что load_descriptor у CLI:
        # безопасный относительный путь вне .claude/ (иначе алиас-коллизия
        # fs_rel / traversal на стороне проекта; T31 fs-mapping r1)
        parts = real_path.split("/")
        if (not real_path or real_path.startswith("/") or "\\" in real_path
                or any(c in ("", ".", "..") for c in parts) or parts[0] == ".claude"):
            die(f"manifest: недопустимый канонический путь: {real_path!r}")
        mode, blob_sha = tree[real_path]
        files[real_path] = {"blob_sha": blob_sha, "mode": mode}
        membership.setdefault(real_path, set()).update(sections)

    for mpath, sections in manifest_paths.items():
        if mpath.startswith("skills/"):
            comps = mpath.split("/")
            if len(comps) < 2:
                die(f"manifest: некорректный skill-путь {mpath}")
            skill_dir = f"skills/{comps[1]}/"
            matched = [p for p in tree if p.startswith(skill_dir)]
            if not matched:
                die(f"manifest: skill {skill_dir} не найден в дереве ref")
            for p in matched:
                add(p, sections)
        else:
            if mpath not in tree:
                die(f"manifest: путь {mpath} не найден в дереве ref")
            add(mpath, sections)

    membership_sorted = {
        p: sorted(s, key=SECTIONS.index if all(x in SECTIONS for x in s) else str)
        for p, s in membership.items()
    }
    return files, membership_sorted


def canonical_json(obj: object) -> str:
    """Канонический sorted-JSON (детерминированная сериализация для digest)."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def compute_digest(files: dict, membership: dict) -> str:
    """manifest_digest = sha256 канонического sorted-JSON files+membership.

    Волатильные поля (commit_sha, built, rev) НЕ входят - равный состав канона
    даёт равный digest (fast-path, дизайн §2, находка 8)."""
    payload = canonical_json({"files": files, "membership": membership})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_descriptor(ref: str, min_cli: int) -> dict:
    manifest_text = (REPO_ROOT / "manifest.yaml").read_text(encoding="utf-8")
    sections = parse_manifest(manifest_text)
    # manifest-путь -> множество секций (membership множественная: один путь в неск. секциях)
    manifest_paths: dict[str, set[str]] = {}
    for section, paths in sections.items():
        for p in paths:
            manifest_paths.setdefault(p, set()).add(section)
    tree = git_ls_tree(ref)
    files, membership = expand_paths(manifest_paths, tree)
    # инъективность fs_rel на case-insensitive ФС проекта: два канон-пути,
    # различающихся лишь регистром, слились бы в один файл (T31 fs-mapping r2)
    seen: dict[str, str] = {}
    for p in files:
        fs = ("" if p.startswith("scripts/") else ".claude/") + p
        key = fs.casefold()
        if key in seen and seen[key] != p:
            die(f"manifest: case-fold коллизия путей {seen[key]!r} и {p!r}")
        seen.setdefault(key, p)
    digest = compute_digest(files, membership)
    return {
        "schema_version": SCHEMA_VERSION,
        "manifest_digest": digest,
        "files": files,
        "membership": membership,
        "min_cli_version": min_cli,
        "plugin_source": None,  # v1 all-delta
    }


def write_descriptor(desc: dict, out_path: Path) -> None:
    # человекочитаемый (отступы), но детерминированный (sort_keys) на диске.
    # digest считается по canonical_json, не по этому представлению.
    text = json.dumps(desc, sort_keys=True, ensure_ascii=True, indent=2) + "\n"
    out_path.write_text(text, encoding="utf-8")


def cmd_build(args: argparse.Namespace) -> int:
    desc = build_descriptor(args.ref, args.min_cli_version)
    out = Path(args.output)
    write_descriptor(desc, out)
    commit = resolve_commit(args.ref)
    sys.stderr.write(
        f"build-lock: {out} schema={desc['schema_version']} "
        f"files={len(desc['files'])} digest={desc['manifest_digest'][:12]} "
        f"commit={commit[:12]} ref={args.ref}\n"
    )
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Server-side gate (R1): пересобрать descriptor от ref и сверить с изданным
    lock-файлом. Расхождение lock-vs-дерево -> отказ (иначе up-to-date файлы не
    тянутся и изменение доедет молча). Сверяется по manifest_digest + files +
    membership; min_cli_version/schema тоже. Возвращает 0 если совпало, 1 если нет."""
    lock_path = Path(args.check)
    if not lock_path.exists():
        die(f"lock-файл не найден: {lock_path}", 2)
    try:
        published = json.loads(lock_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(f"lock-файл не JSON: {e}", 2)
    rebuilt = build_descriptor(args.ref, published.get("min_cli_version", DEFAULT_MIN_CLI_VERSION))
    mismatches = []
    for key in ("schema_version", "manifest_digest", "files", "membership", "plugin_source"):
        if published.get(key) != rebuilt.get(key):
            mismatches.append(key)
    if mismatches:
        sys.stderr.write(
            f"build-lock: GATE FAIL - lock расходится с деревом {args.ref}: "
            f"{', '.join(mismatches)}\n"
        )
        return 1
    sys.stderr.write(f"build-lock: GATE OK - lock соответствует дереву {args.ref}\n")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Генератор release-descriptor canon.lock.json")
    p.add_argument("--ref", default="HEAD", help="git-ref ревизии (тег canon-vN или HEAD)")
    p.add_argument("--output", default="canon.lock.json", help="куда писать descriptor")
    p.add_argument("--min-cli-version", type=int, default=DEFAULT_MIN_CLI_VERSION,
                   help="минимальная версия canon-delta CLI для этого lock")
    p.add_argument("--check", metavar="LOCK",
                   help="режим gate: сверить существующий LOCK с деревом --ref (не писать)")
    args = p.parse_args()
    if args.check:
        return cmd_check(args)
    return cmd_build(args)


if __name__ == "__main__":
    sys.exit(main())
