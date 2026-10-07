#!/usr/bin/env python3
"""
Код возврата 3 - сводка неполная: invalid_lines > 0.
session-cost.py - подсчет токенов Claude Code сессии из транскрипта.

Claude Code пишет транскрипт каждой сессии в
`~/.claude/projects/<encoded-project>/<session-id>.jsonl`. У каждого
assistant-сообщения есть `message.usage` с полями input_tokens,
output_tokens, cache_creation_input_tokens (запись кэша),
cache_read_input_tokens (чтение кэша). Скрипт учитывает последний непустой usage по message.id в каждом
транскрипте и включает субагентов сессии.

Зачем: заполнять токен-строку в смете кейса (часы + токены, без денег) без
ручного `/cost`. `/usage` дает только % лимита, а не сырые токены - тут сырые.

Кодировка имени проекта: Claude Code берет абсолютный путь рабочей папки и
заменяет каждый не-alnum символ на '-' (`/Users/x/My.Proj` ->
`-Users-x-My-Proj`; кириллица и пробелы - тоже по дефису на символ).

Текущая сессия определяется по env CLAUDE_CODE_SESSION_ID (его ставит Claude
Code). Если переменной нет - падаем на свежайшую по mtime с предупреждением
(при параллельных сессиях это ненадежно - тогда --session явно).

Использование:
    session-cost.py                      # текущая сессия (CLAUDE_CODE_SESSION_ID) в проекте по CWD
    session-cost.py --session <id>       # конкретная сессия (по имени файла без .jsonl)
    session-cost.py --file <path.jsonl>  # конкретный файл транскрипта
    session-cost.py --project <dir>      # другой проект (путь к рабочей папке)
    session-cost.py --all-sessions       # суммировать все сессии проекта
    session-cost.py --json               # машиночитаемый вывод

Оговорка по интерпретации: cache_read обычно доминирует - это перечитывание
постоянного контекста (CLAUDE.md, правила, память, схемы тулов) на каждом
ходу, а не "работа по задаче". Показатель реального труда - output (и отчасти
cache_write). Скрипт это помечает в выводе.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import re
import sys
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"

USAGE_FIELDS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_input_tokens",
    "cache_write": "cache_creation_input_tokens",
}


def encode_project(path: Path) -> str:
    """Абсолютный путь рабочей папки -> имя папки в ~/.claude/projects.

    Правило Claude Code: каждый символ вне [A-Za-z0-9] заменяется на '-'
    (без схлопывания). Кириллица и пробелы тоже -> по '-' на символ.
    """
    return re.sub(r"[^a-zA-Z0-9]", "-", str(path.resolve()))


def project_dir(project_path: Path) -> Path:
    return PROJECTS_DIR / encode_project(project_path)


def newest_jsonl(directory: Path) -> Path | None:
    files = sorted(directory.glob("*.jsonl"), key=os.path.getmtime)
    return files[-1] if files else None


def summary(paths: list[Path], messages: int, tokens: dict,
            invalid_lines: int = 0, messages_without_id: int = 0) -> dict:
    return {
        "files": [str(p) for p in paths],
        "messages": messages,
        "tokens": tokens,
        "work_tokens": tokens["output"] + tokens["cache_write"],
        "grand_total": sum(tokens.values()),
        "invalid_lines": invalid_lines,
        "messages_without_id": messages_without_id,
    }


def unique_paths(paths: list[Path]) -> list[Path]:
    return list(dict.fromkeys(p.resolve() for p in paths))


def sum_usage(paths: list[Path]) -> dict:
    paths = unique_paths(paths)
    totals = {k: 0 for k in USAGE_FIELDS}
    messages = invalid_lines = messages_without_id = 0
    for path in paths:
        by_id = {}
        without_id = []
        invalid = 0
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    invalid += 1
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "assistant":
                    continue
                message = obj.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict) or not usage:
                    continue
                message_id = message.get("id")
                if not message_id:
                    without_id.append(usage)
                else:
                    by_id[message_id] = usage
        if invalid:
            sys.stderr.write(
                f"{path}: invalid_lines={invalid} - битые JSON-строки пропущены, "
                "сводка неполная.\n"
            )
        if without_id:
            sys.stderr.write(
                f"{path}: messages_without_id={len(without_id)} - сообщения без ID "
                "учтены отдельно, дедупликация для них невозможна.\n"
            )
        invalid_lines += invalid
        messages_without_id += len(without_id)
        messages += len(by_id) + len(without_id)
        for usage in [*by_id.values(), *without_id]:
            for key, field in USAGE_FIELDS.items():
                totals[key] += usage.get(field, 0) or 0
    return summary(paths, messages, totals, invalid_lines, messages_without_id)


def subagent_paths(paths: list[Path]) -> list[Path]:
    found = []
    for path in paths:
        directory = path.with_suffix("") / "subagents"
        # stat/iterdir сохраняют ошибки доступа; glob может их скрыть.
        try:
            directory.stat()
        except FileNotFoundError:
            continue
        found.extend(sorted(p for p in directory.iterdir() if p.suffix == ".jsonl"))
    return unique_paths(found)


def resolve_paths(args) -> list[Path]:
    if args.file:
        p = Path(args.file)
        if not p.exists():
            sys.exit(f"Файл не найден: {p}")
        return [p]

    pdir = project_dir(Path(args.project) if args.project else Path.cwd())
    if not pdir.exists():
        sys.exit(
            f"Нет папки транскриптов проекта: {pdir}\n"
            f"Проверь путь (--project) или укажи файл напрямую (--file)."
        )

    if args.session:
        p = pdir / f"{args.session}.jsonl"
        if not p.exists():
            sys.exit(f"Нет сессии {args.session} в {pdir}")
        return [p]

    if args.all_sessions:
        files = sorted(pdir.glob("*.jsonl"), key=os.path.getmtime)
        if not files:
            sys.exit(f"В {pdir} нет транскриптов.")
        return files

    # Надежный якорь текущей сессии - env CLAUDE_CODE_SESSION_ID (его ставит
    # Claude Code). "Свежайший по mtime" ненадежен: при параллельных сессиях в
    # одном проекте схватит чужую, которую записали последней.
    env_sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if env_sid:
        p = pdir / f"{env_sid}.jsonl"
        if p.exists():
            return [p]
        sys.stderr.write(
            f"CLAUDE_CODE_SESSION_ID={env_sid}, но {p.name} в проекте нет - "
            f"падаю на свежайшую по mtime.\n"
        )

    latest = newest_jsonl(pdir)
    if latest is None:
        sys.exit(f"В {pdir} нет транскриптов.")
    sys.stderr.write(
        "ВНИМАНИЕ: беру свежайшую сессию по mtime (нет CLAUDE_CODE_SESSION_ID). "
        "Если параллельно открыты другие сессии Claude Code в этом проекте - это "
        "может быть НЕ текущая, тогда укажи --session явно.\n"
    )
    return [latest]


TOKEN_KEYS = ("input", "output", "cache_read", "cache_write", "reasoning")
NATIVE_KEYS = ("session_id", "provider", "begin", "end", "source_file", "prefix_sha256")


class InputError(ValueError):
    pass


def _err(message):
    raise InputError(message)


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value: _err("duplicate JSON key")
        value[key] = item
    return value


def _tokens(value):
    if not isinstance(value, dict): _err("tokens must be an object")
    if set(value) != set(TOKEN_KEYS): _err("tokens must contain the five defined fields")
    for key, item in value.items():
        if item is None and key not in ("input", "output"): continue
        if not _integer(item): _err("token values must be nonnegative integers or null")
    if value["cache_read"] is not None and value["cache_write"] is not None and value["cache_read"] + value["cache_write"] > value["input"]:
        _err("cache breakdown exceeds input")
    if value["reasoning"] is not None and value["reasoning"] > value["output"]:
        _err("reasoning exceeds output")
    return value


def _native(source):
    if not isinstance(source, dict) or set(source) != set(NATIVE_KEYS): _err("invalid native source")
    if not isinstance(source["session_id"], str) or not source["session_id"] or source["provider"] not in ("codex", "claude"):
        _err("invalid native identity")
    if not _integer(source["begin"]) or not _integer(source["end"]) or source["end"] <= source["begin"]: _err("invalid native interval")
    if not isinstance(source["source_file"], str) or not source["source_file"]: _err("invalid native source path")
    if not isinstance(source["prefix_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", source["prefix_sha256"]): _err("invalid prefix hash")


def validate_record(record):
    if not isinstance(record, dict): _err("record must be an object")
    required = {"id", "role", "vendor", "model", "platform", "access", "status", "source", "tokens"}
    if not required <= set(record) or set(record) - required - {"reason", "money"}: _err("invalid record fields")
    for key in ("id", "role", "vendor", "model", "platform"):
        if not isinstance(record[key], str) or not record[key].strip(): _err("record identity fields must be nonempty strings")
    if record["access"] not in ("api", "subscription", "local", "unknown"): _err("invalid access")
    if record["status"] not in ("measured", "unknown"): _err("invalid status")
    if isinstance(record["source"], dict): _native(record["source"])
    elif not isinstance(record["source"], str) or not record["source"].strip(): _err("source required")
    if record["status"] == "measured": _tokens(record["tokens"])
    else:
        if record["tokens"] is not None or not isinstance(record.get("reason"), str) or not record["reason"].strip(): _err("unknown usage needs null tokens and a reason")
    if "money" in record:
        m = record["money"]
        if not isinstance(m, dict) or set(m) != {"amount", "currency", "kind", "source", "as_of"}: _err("invalid money object")
        if not isinstance(m["amount"], str): _err("money amount must be a decimal string")
        try: amount = Decimal(m["amount"])
        except InvalidOperation: _err("invalid money amount")
        if not amount.is_finite() or amount < 0: _err("invalid money amount")
        if not isinstance(m["currency"], str) or not re.fullmatch(r"[A-Z]{3}", m["currency"]): _err("invalid currency")
        if m["kind"] not in ("actual", "estimate"): _err("invalid money kind")
        if m["kind"] == "actual" and record["access"] == "subscription": _err("subscription money cannot be actual")
        if not isinstance(m["source"], str) or not m["source"].strip(): _err("money source required")
        if not isinstance(m["as_of"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", m["as_of"]): _err("invalid money date")
        try: date.fromisoformat(m["as_of"])
        except (ValueError, TypeError): _err("invalid money date")


def task_report(path):
    try: obj = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_json_object)
    except (OSError, UnicodeError, json.JSONDecodeError): _err("cannot read valid ledger JSON")
    if not isinstance(obj, dict) or set(obj) != {"schema_version", "task_id", "records"}: _err("invalid ledger fields")
    if obj["schema_version"] != 1 or isinstance(obj["schema_version"], bool): _err("unsupported schema_version")
    if not isinstance(obj["task_id"], str) or not obj["task_id"].strip() or not isinstance(obj["records"], list): _err("invalid ledger identity or records")
    unique = {}
    intervals = defaultdict(list)
    for rec in obj["records"]:
        validate_record(rec)
        ident = rec["id"]
        if ident in unique:
            if unique[ident] != rec: _err("conflicting duplicate record id")
            continue
        unique[ident] = rec
        if isinstance(rec["source"], dict):
            s = rec["source"]
            key = (s["provider"], s["session_id"], rec["model"])
            for begin, end in intervals[key]:
                if max(begin, s["begin"]) < min(end, s["end"]): _err("overlapping native intervals")
            intervals[key].append((s["begin"], s["end"]))
    grouped = {}
    money_totals = defaultdict(Decimal)
    money_unknown = 0
    for rec in unique.values():
        key = tuple(rec[k] for k in ("role", "vendor", "model", "platform", "access"))
        row = grouped.setdefault(key, {"records": 0, "tokens": {k: 0 for k in TOKEN_KEYS}, "null": set(), "total": 0, "unknown_records": 0, "money": defaultdict(Decimal), "money_unknown_records": 0})
        row["records"] += 1
        if rec["status"] == "unknown":
            row["unknown_records"] += 1; row["money_unknown_records"] += 1; money_unknown += 1
            for k in TOKEN_KEYS: row["null"].add(k)
            continue
        t = rec["tokens"]
        row["total"] += t["input"] + t["output"]
        for k in TOKEN_KEYS:
            if t[k] is None: row["null"].add(k)
            else: row["tokens"][k] += t[k]
        money = rec.get("money")
        if money is None: row["money_unknown_records"] += 1; money_unknown += 1
        else:
            mk = (money["currency"], money["kind"])
            amount = Decimal(money["amount"])
            row["money"][mk] += amount; money_totals[mk] += amount
    rows = []
    for key, value in sorted(grouped.items()):
        rows.append(dict(zip(("role", "vendor", "model", "platform", "access"), key)) | {
            "records": value["records"], "tokens": {k: (None if k in value["null"] else value["tokens"][k]) for k in TOKEN_KEYS},
            "total": value["total"], "unknown_records": value["unknown_records"],
            "money": [{"currency": c, "kind": t, "amount": str(a)} for (c,t),a in sorted(value["money"].items())],
            "money_unknown_records": value["money_unknown_records"]})
    unknown = sum(r["status"] == "unknown" for r in unique.values())
    return {"schema_version": 1, "task_id": obj["task_id"], "rows": rows,
            "measured_tokens": sum(r["total"] for r in rows), "unknown_records": unknown,
            "coverage": "partial" if unknown else "complete",
            "money_totals": [{"currency": c, "kind": k, "amount": str(a)} for (c,k),a in sorted(money_totals.items())],
            "money_unknown_records": money_unknown}


def _strict_jsonl(raw):
    try: text = raw.decode("utf-8")
    except UnicodeDecodeError: _err("source is not UTF-8")
    if text and not text.endswith("\n"): _err("source ends with an incomplete line")
    result = []
    for line in text.splitlines():
        try: value = json.loads(line, object_pairs_hook=_json_object)
        except json.JSONDecodeError: _err("source contains invalid JSON")
        if not isinstance(value, dict): _err("source line must be an object")
        result.append(value)
    return result


def _counter(info):
    if not isinstance(info, dict): _err("invalid usage metadata")
    input_, output = info.get("input_tokens"), info.get("output_tokens")
    if not _integer(input_) or not _integer(output): _err("invalid usage counters")
    cached = info.get("cached_input_tokens"); written = info.get("cache_write_input_tokens"); reasoning = info.get("reasoning_output_tokens")
    if any(x is not None and not _integer(x) for x in (cached, written, reasoning)) or (cached is not None and cached > input_) or (written is not None and written > input_) or (cached is not None and written is not None and cached + written > input_) or (reasoning is not None and reasoning > output): _err("invalid usage breakdown")
    return {"input": input_, "output": output, "cache_read": cached, "cache_write": written, "reasoning": reasoning}


def snapshot(provider, path, since=None):
    path = Path(path).resolve()
    try: raw = path.read_bytes()
    except OSError: _err("cannot read source file")
    lines = _strict_jsonl(raw)
    digest = hashlib.sha256(raw).hexdigest()
    saved = None
    if since:
        try: saved = json.loads(Path(since).read_text(encoding="utf-8"), object_pairs_hook=_json_object)
        except (OSError, UnicodeError, json.JSONDecodeError): _err("cannot read checkpoint")
        if not isinstance(saved, dict) or set(saved) != {"schema_version", "provider", "session_id", "source_file", "offset", "prefix_sha256", "groups"} or saved.get("schema_version") != 1 or isinstance(saved.get("schema_version"), bool) or saved.get("provider") != provider or not _integer(saved.get("offset")) or saved["offset"] > len(raw): _err("invalid checkpoint")
        if saved.get("source_file") != str(path) or hashlib.sha256(raw[:saved["offset"]]).hexdigest() != saved.get("prefix_sha256"): _err("source prefix changed")
        if not isinstance(saved.get("session_id"), str) or not saved["session_id"] or not isinstance(saved.get("groups"), dict): _err("invalid checkpoint identity")
        for mod, group_tokens in saved["groups"].items():
            if not isinstance(mod, str) or not mod: _err("invalid checkpoint model")
            _tokens(group_tokens)
        start = saved["offset"]
    else: start = 0
    if raw[:start] and not raw[:start].endswith(b"\n"): _err("checkpoint is not at a line boundary")
    if provider == "codex":
        sid = None; model = None; cumulative = {k: 0 for k in TOKEN_KEYS}; per_model = defaultdict(lambda: {k: 0 for k in TOKEN_KEYS}); totals_by_model = defaultdict(lambda: {k: 0 for k in TOKEN_KEYS}); touched = set(); optional_seen = defaultdict(set); seen_models = set()
        bytepos = 0; begin = saved["offset"] if saved else 0
        for obj, rawline in zip(lines, raw.splitlines(keepends=True)):
            is_new = bytepos >= begin
            bytepos += len(rawline)
            typ = obj.get("type"); payload = obj.get("payload", {})
            if typ == "session_meta":
                ident = payload.get("id") if isinstance(payload, dict) else None
                if not isinstance(ident, str) or not ident: _err("missing session identity")
                if sid and sid != ident: _err("conflicting session identity")
                sid = ident
            elif typ == "turn_context":
                model = payload.get("model") if isinstance(payload, dict) else None
                if not isinstance(model, str) or not model: _err("missing model")
            elif typ == "event_msg" and isinstance(payload, dict) and payload.get("type") == "token_count":
                info = payload.get("info", {}).get("total_token_usage") if isinstance(payload.get("info"), dict) else None
                curr = _counter(info)
                seen_models.add(model)
                optional_seen[model].update(k for k in ("cache_read", "cache_write", "reasoning") if curr[k] is not None)
                if not model: _err("usage without model")
                old = dict(cumulative)
                delta = {}
                for key, value in curr.items():
                    if value is None: delta[key] = None
                    else:
                        if value < old[key]: _err("usage counter rollback")
                        delta[key] = value - old[key]
                        cumulative[key] = value
                if any(v not in (0, None) for v in delta.values()):
                    for key, value in delta.items():
                        if value is not None:
                            totals_by_model[model][key] += value
                            if is_new: per_model[model][key] += value
                if is_new and any(v not in (0, None) for v in delta.values()):
                    touched.add(model)
        if not sid: _err("missing session identity")
        groups = {m: {k: totals_by_model[m][k] for k in TOKEN_KEYS} for m in seen_models}
        for mod in groups:
            for key in ("cache_read", "cache_write", "reasoning"):
                if key not in optional_seen[mod]: groups[mod][key] = None
    elif provider == "claude":
        sid = None; by_id = {}
        for obj in lines:
            if obj.get("type") != "assistant": continue
            message = obj.get("message")
            if not isinstance(message, dict): _err("invalid assistant message")
            ident, current, mod = obj.get("sessionId"), message.get("id"), message.get("model")
            if not isinstance(ident, str) or not ident: _err("missing session identity")
            if sid and sid != ident: _err("conflicting session identity")
            sid = ident
            if not isinstance(current, str) or not current or not isinstance(mod, str) or not mod: _err("missing message id or model")
            usage = message.get("usage")
            if not isinstance(usage, dict) or not usage: continue
            base_input = usage.get("input_tokens"); cache_read = usage.get("cache_read_input_tokens", 0); cache_write = usage.get("cache_creation_input_tokens", 0); output = usage.get("output_tokens")
            if not _integer(base_input) or not _integer(output) or not _integer(cache_read) or not _integer(cache_write): _err("invalid Claude usage")
            values = {"input": base_input + cache_read + cache_write, "output": output,
                      "cache_read": cache_read, "cache_write": cache_write, "reasoning": None}
            if current in by_id and by_id[current][0] != mod: _err("conflicting model for message")
            if current in by_id and any(values[k] < by_id[current][1][k] for k in ("input", "output", "cache_read", "cache_write")): _err("Claude usage rollback")
            by_id[current] = (mod, values)
        if not sid: _err("missing session identity")
        per_model = defaultdict(lambda: {k: 0 for k in TOKEN_KEYS}); touched = set()
        for mod, values in by_id.values():
            for key in TOKEN_KEYS:
                if values[key] is None: continue
                per_model[mod][key] += values[key]
            touched.add(mod)
        groups = {m: {k: per_model[m][k] for k in TOKEN_KEYS} for m in touched}
    else: _err("unsupported provider")
    if saved and sid != saved["session_id"]: _err("session identity changed")
    if saved:
            oldgroups = saved["groups"]
            for mod, prior in oldgroups.items():
                if mod not in groups: _err("checkpoint model missing")
                for key in TOKEN_KEYS:
                    if prior.get(key) is not None and (not _integer(prior[key]) or groups[mod][key] is None or groups[mod][key] < prior[key]): _err("usage counter rollback")
            for mod, cur in list(groups.items()):
                prev = oldgroups.get(mod, {k: (0 if cur[k] is not None else None) for k in TOKEN_KEYS})
                per_model[mod] = {k: (None if cur[k] is None or prev.get(k) is None else cur[k] - prev[k]) for k in TOKEN_KEYS}
            touched = {m for m in groups if any(v not in (0, None) for v in per_model[m].values())}
    checkpoint = {"schema_version": 1, "provider": provider, "session_id": sid, "source_file": str(path), "offset": len(raw), "prefix_sha256": digest, "groups": groups}
    if not saved:
        print(json.dumps(checkpoint, ensure_ascii=False, indent=2)); return 0
    records = []
    for mod in sorted(touched):
        toks = per_model[mod]
        # Optional Claude reasoning is unknown; Codex fields are native counters.
        rec_tokens = dict(toks)
        if provider == "claude": rec_tokens["reasoning"] = None
        begin = saved["offset"]
        if len(raw) <= begin: continue
        source = {"session_id": sid, "provider": provider, "begin": begin, "end": len(raw), "source_file": str(path), "prefix_sha256": hashlib.sha256(raw[:begin]).hexdigest()}
        records.append({"id": f"{provider}:{sid}:{begin}:{len(raw)}:{mod}", "role": "author", "vendor": provider, "model": mod, "platform": "cli", "access": "subscription" if provider == "claude" else "api", "status": "measured", "source": source, "tokens": rec_tokens})
    print(json.dumps({"schema_version": 1, "provider": provider, "session_id": sid, "source_file": str(path), "begin": saved["offset"], "end": len(raw), "prefix_sha256": digest, "records": records, "checkpoint": checkpoint}, ensure_ascii=False, indent=2)); return 0


def render_task(data):
    def esc(x): return str(x).replace("\n", " ").replace("|", "\\|")
    print(f"Task {esc(data['task_id'])}: {data['coverage']} coverage; measured input+output tokens={data['measured_tokens']}; unknown records={data['unknown_records']}")
    print("| role | vendor | model | platform | access | input | cache_read | cache_write | output | reasoning | total | money | quality |")
    print("|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---|---|")
    for r in data["rows"]:
        t = r["tokens"]; money = ", ".join(f"{m['amount']} {m['currency']} {m['kind']}" for m in r["money"]) or "unknown"
        vals = [r[k] for k in ("role", "vendor", "model", "platform", "access")]+[t[k] if t[k] is not None else "unknown" for k in TOKEN_KEYS]+[r["total"], money, "partial" if r["unknown_records"] else "measured"]
        print("| " + " | ".join(esc(v) for v in vals) + " |")


def new_modes(args):
    if args.task_ledger:
        if args.snapshot or args.provider or args.since or args.file or args.session or args.project or args.all_sessions: _err("incompatible task-ledger options")
        data = task_report(args.task_ledger)
        if args.json: print(json.dumps(data, ensure_ascii=False, indent=2))
        else: render_task(data)
        return 3 if data["coverage"] == "partial" else 0
    if args.snapshot:
        if not args.provider or not args.file or args.session or args.project or args.all_sessions or args.json: _err("--snapshot needs --provider and --file only")
        return snapshot(args.provider, args.file, args.since)
    if args.provider or args.since: _err("--provider/--since require --snapshot")
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Подсчет токенов Claude Code сессии из транскрипта.")
    ap.add_argument("--task-ledger", help="проверить и свести ledger задачи")
    ap.add_argument("--snapshot", action="store_true", help="снять явный локальный прирост usage")
    ap.add_argument("--provider", choices=("codex", "claude"))
    ap.add_argument("--since", help="предыдущий checkpoint для --snapshot")
    ap.add_argument("--file", help="путь к конкретному .jsonl транскрипта")
    ap.add_argument("--session", help="id сессии (имя файла без .jsonl) в текущем/указанном проекте")
    ap.add_argument("--project", help="путь к рабочей папке проекта (по умолчанию - CWD)")
    ap.add_argument("--all-sessions", action="store_true", help="суммировать все сессии проекта")
    ap.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    args = ap.parse_args()

    if args.task_ledger or args.snapshot or args.provider or args.since:
        try:
            result = new_modes(args)
            return 0 if result is None else result
        except (InputError, OSError, UnicodeError, TypeError, KeyError, ValueError) as exc:
            sys.stderr.write(f"Ошибка учета задачи: {exc}\n")
            return 2

    try:
        paths = unique_paths(resolve_paths(args))
        children = subagent_paths(paths)
        main_paths = [p for p in paths if p not in children]
        main_result = sum_usage(main_paths)
        subagents = sum_usage(children)
    except (OSError, UnicodeError) as exc:
        sys.stderr.write(f"Ошибка чтения транскриптов: {exc}\n")
        return 1

    tokens = {k: main_result["tokens"][k] + subagents["tokens"][k]
              for k in USAGE_FIELDS}
    result = summary(
        main_paths + children,
        main_result["messages"] + subagents["messages"], tokens,
        main_result["invalid_lines"] + subagents["invalid_lines"],
        main_result["messages_without_id"] + subagents["messages_without_id"],
    )
    result.update(main=main_result, subagents=subagents)
    exit_code = 3 if result["invalid_lines"] else 0
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return exit_code

    for label, group in (("Основной контекст (main)", main_result),
                         ("Субагенты (subagents)", subagents),
                         ("Общий итог", result)):
        t = group["tokens"]
        print(f"{label}: messages={group['messages']}, "
              f"input={t['input']}, output={t['output']}, "
              f"cache_read={t['cache_read']}, cache_write={t['cache_write']}, "
              f"work={group['work_tokens']}, grand_total={group['grand_total']}")
        print(f"  invalid_lines={group['invalid_lines']}, "
              f"messages_without_id={group['messages_without_id']}")
    if result["invalid_lines"]:
        print("Сводка неполная: битые JSON-строки пропущены.")
    if result["messages_without_id"]:
        print("Дедупликация ограничена: сообщения без ID учтены отдельно.")
    print("work = output + cache_write; cache_read - перечитывание контекста.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
