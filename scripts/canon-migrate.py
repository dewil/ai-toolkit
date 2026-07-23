#!/usr/bin/env python3
"""canon-migrate.py - один раз расщепить старый canon.yaml на intent/state/ledger
(этап 8, §6 миграция, R12).

Старая схема: монолит `.claude/canon.yaml` (project_type + files + file_hashes[sha256]
+ canon-map). Новая: intent(человек) / state.json(машина, git-blob-sha) / ledger(harvester).

КЛЮЧЕВОЕ: старые file_hashes - это sha256(содержимое), НЕ git-blob-sha, поэтому НЕ
переносятся. state.file_hashes сидится заново перехешированием ЛОКАЛЬНЫХ файлов
git-алгоритмом (base=local): локальные файлы = текущий канон на момент synced_at, значит
первый `sync` даст up-to-date для совпавших и outdated-ff для сдвинувшихся - корректный
bootstrap. applied_release=None (пин узнается на первом sync).

Дедуп pending (§6/F): старый `upstream_pending` (записи БЕЗ candidate-id) + внешний
harvester-lifecycle сводятся в единый ledger; синтетический candidate-id =
sha256(<содержимое брифа>) для несопоставленных из старого источника.

Запуск:
  canon-migrate.py --root <project>            # пишет intent/state/ledger рядом с canon.yaml
  canon-migrate.py --root <project> --force    # перезаписать существующие intent/state
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path, PurePosixPath

# переиспользуем git_blob_sha/read_local/empty_state из canon-delta (имя с дефисом -> importlib)
_CD_PATH = Path(__file__).resolve().parent / "canon-delta.py"
_spec = importlib.util.spec_from_file_location("canon_delta", _CD_PATH)
cd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cd)

_LIST_KEYS = ("project_type", "files", "skip_sync", "local_only", "overrides", "upstream_pending")


def die(msg: str, code: int = 2) -> None:
    sys.stderr.write(f"canon-migrate: {msg}\n")
    sys.exit(code)


def parse_old_canon(text: str) -> dict:
    """Извлечь из старого canon.yaml только нужные поля. Вложенные map (file_hashes,
    canon) игнорируются - коллектор берет лишь '- item' под интересующими top-key."""
    out: dict = {k: [] for k in _LIST_KEYS}
    out["track"] = None
    current_top: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if not line.strip() or line.strip().startswith("#"):
            continue
        indented = line[0] in " \t"
        stripped = line.strip()
        if not indented and ":" in stripped:
            key, _, rest = stripped.partition(":")
            key = key.strip()
            rest = rest.strip()
            current_top = key
            if rest.startswith("[") and rest.endswith("]"):
                inner = rest[1:-1].strip()
                vals = [x.strip() for x in inner.split(",") if x.strip()]
                if key in out:
                    out[key] = vals
            elif rest and key == "track":
                out["track"] = rest
            continue
        if stripped.startswith("- ") and current_top in _LIST_KEYS:
            item = stripped[2:].strip()
            if " #" in item:
                item = item.split(" #", 1)[0].strip()
            if item:
                out[current_top].append(item)
    return out


def build_intent(old: dict) -> str:
    """canon.intent.yaml - человеческие поля. project_type/track всегда; исключения -
    только если непусты (не плодим пустые ключи)."""
    lines = []
    pt = old.get("project_type") or []
    lines.append("project_type: [" + ", ".join(pt) + "]")
    lines.append(f"track: {old.get('track') or 'stable'}")
    for key in ("skip_sync", "local_only", "overrides"):
        vals = old.get(key) or []
        if vals:
            lines.append(f"{key}:")
            lines.extend(f"  - {v}" for v in vals)
    return "\n".join(lines) + "\n"


def build_state(old: dict, root: Path) -> dict:
    """state.json с file_hashes, перехешированными git-blob-sha из ЛОКАЛЬНЫХ файлов
    (base=local bootstrap). Отсутствующие локально пути пропускаются (всплывут как
    missing-local на первом sync). symlink на месте канон-файла тоже пропускается."""
    state = cd.empty_state()
    fh: dict = {}
    skipped = []
    for path in old.get("files") or []:
        # небезопасный/`.claude/`-префиксный legacy-путь не сидим (validate_rel:
        # traversal и алиас-коллизия fs_rel; T31 fs-mapping r1)
        if cd.validate_rel(path):
            skipped.append(path)
            continue
        local = cd.read_local(root, path)
        if not local["exists"] or local["symlink"]:
            skipped.append(path)
            continue
        fh[path] = {"sha": local["sha"], "mode": local["mode"]}
    state["file_hashes"] = fh
    return state, skipped


def _slug_candidates(entry: str) -> list[str]:
    """Кандидаты слага для канон-пути, от специфичного к общему.

    Контракт слага (harvest / 4e синка): имя брифа выводится из канон-пути;
    skills/foo/SKILL.md -> skills-foo, migrations/sync-from-canon.prompt.md ->
    migrations-sync-from-canon, rules/bar.md -> rules-bar либо bar. Срезаются
    только известные расширения (.md/.py/.yaml/...) плюс суффикс .prompt -
    точка внутри имени не разделитель: rules/python3.12-policy.md ->
    python3.12-policy, а не python3. Директорийный вариант идет ПЕРВЫМ:
    голый stem может привязать запись к чужому брифу (rules/bar.md и
    agents/bar.md делят bar.md).
    """
    p = PurePosixPath(entry)
    stem = p.name
    for ext in (".md", ".py", ".yaml", ".yml", ".json"):
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
            break
    if stem.endswith(".prompt"):
        stem = stem[: -len(".prompt")]
    dirs = list(p.parts[:-1])
    if stem == "SKILL" and dirs:
        return ["-".join(dirs)]
    cands = []
    if dirs:
        cands.append("-".join([*dirs, stem]))
    cands.append(stem)
    return cands


def _legacy_brief_path(root: Path, entry: str) -> str:
    """Путь брифа для legacy-записи upstream_pending.

    Legacy-контракт хранит канон-путь кандидата (skills/foo/SKILL.md), а сам
    бриф лежит в toolkit-log/upstream-pending/<slug>.md. Копировать строку в
    brief_path как есть нельзя: schema-aware sync не найдет по ней файла,
    сочтет запись осиротевшей и уничтожит ее. Если строка уже указывает на
    существующий файл в toolkit-log/ - берем ее; иначе ищем бриф по
    кандидатам слага; не нашли - оставляем строку как есть (хуже не делаем).
    """
    if entry.startswith("toolkit-log/") and (root / entry).is_file():
        return entry
    for slug in _slug_candidates(entry):
        cand = f"toolkit-log/upstream-pending/{slug}.md"
        if (root / cand).is_file():
            return cand
    return entry


def _read_brief(root: Path, brief: str) -> bytes:
    """Байты брифа; нет файла / не читается - синтетика из самого пути."""
    bp = root / brief
    try:
        return bp.read_bytes() if bp.is_file() else brief.encode("utf-8")
    except OSError:
        return brief.encode("utf-8")


def build_ledger(old: dict, root: Path, external: list[dict] | None) -> dict:
    """Свести старый upstream_pending (без candidate-id) и внешний harvester-lifecycle
    в единый ledger. Дедуп по candidate-id: старому присваивается синтетический
    sha256(<содержимое брифа>); при совпадении с внешним берется внешний id.

    Слаг-коллизия (несколько legacy-записей резолвятся в один бриф): бриф
    достается той записи, чей канон-путь упомянут в тексте брифа РАНЬШЕ других
    (шаблон брифа дублирует целевой путь заголовком в первой строке; чужие
    пути в rationale стоят позже) - привязка по атрибуции, не по порядку
    списка; бриф никого не называет - первой по порядку. Остальные записи
    группы сохраняют исходный канон-путь (кандидат не теряется молча). Если
    путь брифа занят harvester-записью, выбранная запись считается тем же
    кандидатом и в ledger не дублируется."""
    external = external or []
    by_id: dict[str, dict] = {}
    by_path: dict[str, str] = {}  # brief_path -> candidate_id (дедуп по пути)
    for e in external:
        cid = e.get("candidate_id")
        if cid:
            by_id[cid] = {"candidate_id": cid, "brief_path": e.get("brief_path"), "source": "harvester"}
            if e.get("brief_path"):
                by_path[e["brief_path"]] = cid

    # legacy-записи: нормализуем (запись бывает рукой занесенным объектом -
    # берем из нее brief_path, пустую пропускаем, не роняя миграцию) и
    # группируем по резолвнутому брифу
    entries: list[str] = []
    for raw in old.get("upstream_pending") or []:
        if isinstance(raw, dict):
            raw = str(raw.get("brief_path") or "")
            if not raw:
                continue
        entries.append(str(raw))
    groups: dict[str, list[str]] = {}
    for entry in entries:
        groups.setdefault(_legacy_brief_path(root, entry), []).append(entry)

    def add_legacy(entry: str, brief: str) -> None:
        cid = hashlib.sha256(_read_brief(root, brief)).hexdigest()
        if cid in by_id:
            return  # уже есть из harvester (тот же контент) - дубль отбрасываем
        by_id[cid] = {"candidate_id": cid, "brief_path": brief, "source": "legacy-canon-yaml"}
        by_path[brief] = cid

    for brief, group in groups.items():
        content = _read_brief(root, brief)
        # владелец брифа - запись, чей канон-путь упомянут в брифе РАНЬШЕ
        # других (шаблон дублирует целевой путь заголовком в первой строке;
        # упоминание чужого пути в rationale стоит позже, порядок legacy-списка
        # роли не играет); бриф никого не называет (рукописный, без шаблона) -
        # первая по порядку
        mentioned = []
        for i, e in enumerate(group):
            pos = content.find(e.encode("utf-8"))
            if pos != -1:
                mentioned.append((pos, i))
        owner_idx = min(mentioned)[1] if mentioned else 0
        owner = group[owner_idx]
        rest = group[:owner_idx] + group[owner_idx + 1:]
        if brief in by_path:
            # бриф уже привязан (harvester либо более ранняя legacy-группа -
            # возможно при совпадении fallback-путей): владелец - тот же
            # кандидат, дубль не плодим; остальные сохраняют канон-путь
            pass
        else:
            add_legacy(owner, brief)
        for entry in rest:
            add_legacy(entry, entry)  # бриф чужой - оставляем канон-путь, не гадаем
    return {"upstream_pending": sorted(by_id.values(), key=lambda r: r["candidate_id"])}


def cmd_migrate(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    claude = root / ".claude"
    old_path = Path(args.canon) if args.canon else claude / "canon.yaml"
    if not old_path.exists():
        die(f"старый canon.yaml не найден: {old_path}")
    intent_path = claude / "canon.intent.yaml"
    state_path = claude / "canon.state.json"
    ledger_path = claude / "canon.ledger.json"
    if not args.force and (intent_path.exists() or state_path.exists()):
        die("intent/state уже существуют (--force чтобы перезаписать)", 2)

    old = parse_old_canon(old_path.read_text(encoding="utf-8"))
    if not old.get("files"):
        die("в старом canon.yaml нет секции files - нечего мигрировать")

    external = None
    if args.harvester_ledger:
        hp = Path(args.harvester_ledger)
        if hp.exists():
            external = json.loads(hp.read_text(encoding="utf-8")).get("upstream_pending", [])

    intent_text = build_intent(old)
    state, skipped = build_state(old, root)
    ledger = build_ledger(old, root, external)

    cd.atomic_write_text(intent_path, intent_text)
    cd.save_state(state_path, state)
    cd.atomic_write_text(ledger_path, json.dumps(ledger, sort_keys=True, ensure_ascii=True, indent=2) + "\n")

    sys.stderr.write(
        f"canon-migrate: intent={intent_path.name} state={state_path.name}"
        f"({len(state['file_hashes'])} файлов, {len(skipped)} пропущено)"
        f" ledger={ledger_path.name}({len(ledger['upstream_pending'])} pending)\n"
    )
    if skipped:
        sys.stderr.write("  пропущены (нет локально / symlink): " + ", ".join(skipped) + "\n")
    print(json.dumps({
        "intent": str(intent_path), "state": str(state_path), "ledger": str(ledger_path),
        "migrated_files": len(state["file_hashes"]), "skipped": skipped,
        "pending": len(ledger["upstream_pending"]),
    }, ensure_ascii=True, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Миграция canon.yaml -> intent/state/ledger")
    p.add_argument("--root", required=True, help="корень проекта (содержит .claude/canon.yaml)")
    p.add_argument("--canon", help="путь к старому canon.yaml (дефолт: <root>/.claude/canon.yaml)")
    p.add_argument("--harvester-ledger", help="внешний harvester-ledger JSON для дедупа pending")
    p.add_argument("--force", action="store_true", help="перезаписать существующие intent/state")
    p.set_defaults(func=cmd_migrate)
    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
