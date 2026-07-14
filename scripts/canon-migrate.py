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
from pathlib import Path

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
        local = cd.read_local(root, path)
        if not local["exists"] or local["symlink"]:
            skipped.append(path)
            continue
        fh[path] = {"sha": local["sha"], "mode": local["mode"]}
    state["file_hashes"] = fh
    return state, skipped


def build_ledger(old: dict, root: Path, external: list[dict] | None) -> dict:
    """Свести старый upstream_pending (без candidate-id) и внешний harvester-lifecycle
    в единый ledger. Дедуп по candidate-id: старому присваивается синтетический
    sha256(<содержимое брифа>); при совпадении с внешним берется внешний id."""
    external = external or []
    by_id: dict[str, dict] = {}
    for e in external:
        cid = e.get("candidate_id")
        if cid:
            by_id[cid] = {"candidate_id": cid, "brief_path": e.get("brief_path"), "source": "harvester"}
    for brief in old.get("upstream_pending") or []:
        bp = (root / brief)
        try:
            content = bp.read_bytes() if bp.exists() else brief.encode("utf-8")
        except OSError:
            content = brief.encode("utf-8")
        cid = hashlib.sha256(content).hexdigest()
        if cid in by_id:
            continue  # уже есть из harvester - дубль отбрасываем
        by_id[cid] = {"candidate_id": cid, "brief_path": brief, "source": "legacy-canon-yaml"}
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
