#!/usr/bin/env python3
"""
Локальный snapshot расшифровок встреч проекта из mymeet.ai.

Тянет MD-отчеты новых ПРОЕКТНЫХ встреч через REST API mymeet.ai и
раскладывает их по папкам Встречи/ по правилам из проектного конфига.
Аккаунт mymeet общий (встречи не только этого проекта), поэтому скрипт
работает по whitelist: качает только то, что подошло под правило, а
непроектное (не подошедшее ни под одно правило) НЕ трогает - пишет строкой
в review-файл, чтобы человек глазами решил, проектная встреча или нет.

JSON-отчет (word_timings / chapters / метаданные) по умолчанию НЕ качается:
для рутины хватает MD, а JSON на больших объемах - оверхед. Он доступен по
требованию (это API, перекачать можно в любой момент):
    python3 scripts/mymeet-snapshot.py --json <meeting_id>

Конфиг разделен на две части:
  - Секрет (api_key, base_url, опц. use_curl) - в
    ~/.config/mymeet-snapshot/auth.json, один раз на устройство. Не коммитить
    (проект на Yandex.Disk). use_curl: true - если Python не видит CA-бандл и
    падает на SSL (типично для python.org-сборки на macOS); запросы пойдут
    через curl, который берет сертификат из системного хранилища.
  - Проектные параметры - в .mymeet-snapshot.json в корне проекта
    {meetings_root, rules: [{match, dest}], review_file}. Не секрет.

Что нового скрипт уже видел - помнит в <meetings_root>/_mymeet-index.json
(meeting_id -> {title, date, dest, file}); повторно ничего не качает.
"Дельты" для дейлика отдельным скриптом не нужны: новые встречи = то, что
этот прогон только что скачал (печатается в конце), а не diff мутирующего
файла, как у telegram/redmine.

Основной режим - точечно по id (id виден в URL встречи на mymeet, --pull
принимает и голый uuid, и URL целиком):
    python3 scripts/mymeet-snapshot.py --pull <ID|URL>  # скачать одну встречу
    python3 scripts/mymeet-snapshot.py --json <ID|URL>  # добрать JSON встречи

Прочее:
    python3 scripts/mymeet-snapshot.py            # без аргументов - печатает справку
    python3 scripts/mymeet-snapshot.py --list     # dry-run: что нашлось и куда ляжет
    python3 scripts/mymeet-snapshot.py --seed     # baseline: пометить текущие встречи
                                                  #   виденными, не качая
    python3 scripts/mymeet-snapshot.py --all      # забрать ВСЕ новые разом (балк)

Балк по умолчанию НЕ запускается намеренно: аккаунт mymeet общий на несколько
проектов, голый прогон не должен нагребать чужое. Рабочий цикл - точечный --pull
по id из URL (имена спикеров правятся на mymeet до забора).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUTH_DIR = Path.home() / ".config" / "mymeet-snapshot"
AUTH_PATH = AUTH_DIR / "auth.json"
PROJECT_CONFIG_PATH = PROJECT_ROOT / ".mymeet-snapshot.json"

DEFAULT_BASE_URL = "https://backend.mymeet.ai"
PER_PAGE = 50

# Имя поля даты в ответе списка доку точно не зафиксировал - перебираем
# частые варианты. Первый прогон с --list покажет реальные ключи.
DATE_KEYS = (
    "date", "meeting_date", "created_at", "createdAt", "created",
    "start_time", "started_at", "scheduled_at", "scheduled_for", "datetime",
)
TITLE_KEYS = ("title", "name", "meeting_name", "meeting_title")
ID_KEYS = ("meeting_id", "id", "uuid")
STATUS_KEYS = ("status", "state")


def load_auth() -> dict:
    if not AUTH_PATH.exists():
        sys.stderr.write(
            f"Нет общего конфига {AUTH_PATH}.\n"
            "Настрой доступ - см. скилл mymeet-snapshot.\n"
        )
        sys.exit(2)
    with AUTH_PATH.open(encoding="utf-8") as f:
        auth = json.load(f)
    if not auth.get("api_key"):
        sys.stderr.write(f"В {AUTH_PATH} не заполнено поле api_key\n")
        sys.exit(2)
    auth["base_url"] = (auth.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    auth.setdefault("use_curl", False)
    return auth


def load_project_config() -> dict:
    if not PROJECT_CONFIG_PATH.exists():
        sys.stderr.write(
            f"Нет проектного конфига {PROJECT_CONFIG_PATH}.\n"
            "Формат - см. скилл mymeet-snapshot.\n"
        )
        sys.exit(2)
    with PROJECT_CONFIG_PATH.open(encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("meetings_root", "Встречи")
    cfg.setdefault("rules", [])
    cfg.setdefault("review_file", f"{cfg['meetings_root']}/_mymeet-review.txt")
    return cfg


def _build_url(auth: dict, path: str, params: dict | None) -> str:
    url = f"{auth['base_url']}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return url


def _curl_bytes(auth: dict, url: str, timeout: int) -> bytes:
    # Python.framework на macOS не имеет CA-бандла и падает на валидном
    # публичном сертификате; curl берет сертификат из системного хранилища.
    result = subprocess.run(
        ["curl", "-sS", "--fail", "-A", "mymeet-snapshot",
         "-H", f"X-API-KEY: {auth['api_key']}", url],
        check=True, capture_output=True, timeout=timeout,
    )
    return result.stdout


def api_get(auth: dict, path: str, params: dict | None = None):
    """JSON-ответ эндпоинтов /api/*. Возвращает разобранный объект."""
    url = _build_url(auth, path, params)
    if auth.get("use_curl"):
        return json.loads(_curl_bytes(auth, url, 60))
    req = urllib.request.Request(
        url,
        headers={"X-API-KEY": auth["api_key"], "User-Agent": "mymeet-snapshot"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def api_get_bytes(auth: dict, path: str, params: dict | None = None) -> tuple[bytes, str]:
    """Сырой ответ (для download). Возвращает (тело, content-type)."""
    url = _build_url(auth, path, params)
    if auth.get("use_curl"):
        return _curl_bytes(auth, url, 120), ""
    req = urllib.request.Request(
        url,
        headers={"X-API-KEY": auth["api_key"], "User-Agent": "mymeet-snapshot"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read(), resp.headers.get("Content-Type", "")


_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def extract_meeting_id(arg: str) -> str:
    """Принимает голый meeting_id или URL встречи mymeet (id берется из адреса)
    и возвращает чистый uuid. Если uuid не нашелся - вернет arg как есть."""
    m = _UUID_RE.search(arg or "")
    return m.group(0) if m else arg


def pick(meeting: dict, keys) -> str | None:
    for k in keys:
        v = meeting.get(k)
        if v not in (None, ""):
            return v
    return None


def iter_meetings(auth: dict):
    """Пагинация по all-meetings (page с 0, perPage)."""
    page = 0
    while True:
        data = api_get(
            auth,
            "/api/workspaces/active/all-meetings",
            {"page": page, "perPage": PER_PAGE},
        )
        # ответ может быть голым списком или объектом-оберткой
        if isinstance(data, dict):
            batch = (
                data.get("followups")
                or data.get("meetings")
                or data.get("items")
                or data.get("data")
                or data.get("results")
                or []
            )
        else:
            batch = data
        if not batch:
            break
        for m in batch:
            yield m
        if len(batch) < PER_PAGE:
            break
        page += 1


def parse_date(meeting: dict) -> datetime | None:
    raw = pick(meeting, DATE_KEYS)
    if raw is None:
        return None
    # epoch (сек или мс)
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.isdigit()):
        ts = float(raw)
        if ts > 1e12:  # миллисекунды
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    # ISO-строка
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        pass
    # RFC 2822 / HTTP-date: "Thu, 25 Jun 2026 12:02:06 GMT"
    try:
        return parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return None


def match_rule(title: str, rules: list[dict]) -> dict | None:
    low = (title or "").lower()
    for rule in rules:
        for sub in rule.get("match", []):
            if sub.lower() in low:
                return rule
    return None


def build_dest(dest_tpl: str, dt: datetime | None) -> str:
    if dt is None:
        return dest_tpl  # шаблон без даты - placeholders останутся, отсечется раньше
    return (
        dest_tpl
        .replace("{YYYY}", f"{dt.year:04d}")
        .replace("{MM}", f"{dt.month:02d}")
        .replace("{DD}", f"{dt.day:02d}")
    )


def target_file(meetings_root: Path, dest_sub: str, dt: datetime, mid: str,
                index: dict) -> Path:
    """Путь файла ГГГГ-ММ-ДД.md, при коллизии - -2/-3. Если этот meeting_id
    уже привязан к файлу - возвращает его (идемпотентность)."""
    folder = meetings_root / dest_sub
    base = f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
    known = index.get("meetings", {}).get(mid, {}).get("file")
    if known:
        return PROJECT_ROOT / known
    candidate = folder / f"{base}.md"
    n = 2
    existing_files = {
        m.get("file") for m in index.get("meetings", {}).values()
    }
    while candidate.exists() or str(candidate.relative_to(PROJECT_ROOT)) in existing_files:
        candidate = folder / f"{base}-{n}.md"
        n += 1
    return candidate


def download_md(auth: dict, mid: str) -> str:
    body, ctype = api_get_bytes(
        auth, "/api/storage/download", {"meeting_id": mid, "format": "md"}
    )
    # download может вернуть либо сам файл, либо JSON со ссылкой на storage
    if "application/json" in ctype.lower():
        try:
            obj = json.loads(body)
        except ValueError:
            return body.decode("utf-8", "replace")
        link = obj.get("url") or obj.get("download_url") or obj.get("link")
        if link:
            with urllib.request.urlopen(link, timeout=120) as resp:
                return resp.read().decode("utf-8", "replace")
        # JSON без ссылки - вернем как есть
        return body.decode("utf-8", "replace")
    return body.decode("utf-8", "replace")


def place_meeting(auth: dict, cfg: dict, meetings_root: Path, index: dict,
                  m: dict) -> str | None:
    """Скачать MD одной встречи и положить по правилам. Возвращает rel-путь
    или None, если встреча не подошла под правила / без даты."""
    mid = str(pick(m, ID_KEYS))
    title = pick(m, TITLE_KEYS) or ""
    dt = parse_date(m)
    rule = match_rule(title, cfg["rules"])
    if rule is None or dt is None:
        return None
    dest_sub = build_dest(rule["dest"], dt)
    dest_file = target_file(meetings_root, dest_sub, dt, mid, index)
    md = download_md(auth, mid)
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    dest_file.write_text(md, encoding="utf-8")
    rel = str(dest_file.relative_to(PROJECT_ROOT))
    index.setdefault("meetings", {})[mid] = {
        "title": title,
        "date": dt.isoformat(),
        "dest": rule["dest"],
        "file": rel,
    }
    return rel


def load_index(meetings_root: Path) -> dict:
    path = meetings_root / "_mymeet-index.json"
    if path.exists():
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    return {"meetings": {}}


def save_index(meetings_root: Path, index: dict) -> None:
    path = meetings_root / "_mymeet-index.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


def cmd_seed(auth: dict, cfg: dict) -> int:
    """Отметить все текущие встречи аккаунта как "уже виденные" без скачивания.
    Базовая точка: исторические встречи (которые уже скачаны вручную) больше
    не будут тянуться, дальше pull берет только новые."""
    meetings_root = PROJECT_ROOT / cfg["meetings_root"]
    index = load_index(meetings_root)
    added = 0
    for m in iter_meetings(auth):
        mid = pick(m, ID_KEYS)
        if not mid:
            continue
        mid = str(mid)
        if mid in index.get("meetings", {}):
            continue
        dt = parse_date(m)
        index.setdefault("meetings", {})[mid] = {
            "title": pick(m, TITLE_KEYS) or "",
            "date": dt.isoformat() if dt else None,
            "seeded": True,
        }
        added += 1
    meetings_root.mkdir(parents=True, exist_ok=True)
    save_index(meetings_root, index)
    total = len(index.get("meetings", {}))
    print(f"OK: baseline - отмечено как виденные {added} встреч (всего в индексе {total}).")
    print("Скачано: 0. Дальше pull возьмет только новые встречи.")
    return 0


def cmd_pull(auth: dict, cfg: dict, target_id: str) -> int:
    """Точечно скачать одну встречу по meeting_id (для теста или добора из
    review). Размещение - по тем же правилам, что и обычный pull."""
    meetings_root = PROJECT_ROOT / cfg["meetings_root"]
    index = load_index(meetings_root)
    for m in iter_meetings(auth):
        if str(pick(m, ID_KEYS)) == target_id:
            title = pick(m, TITLE_KEYS) or ""
            try:
                rel = place_meeting(auth, cfg, meetings_root, index, m)
            except Exception as exc:
                print(f"!! {target_id} ({title}): {exc}", file=sys.stderr)
                return 1
            meetings_root.mkdir(parents=True, exist_ok=True)
            save_index(meetings_root, index)
            if rel:
                print(f"OK: {title}\n    -> {rel}")
            else:
                print(f"Встреча найдена ({title}), но не подошла под правила "
                      "или без даты - не размещена.")
            return 0
    print(f"Встреча {target_id} не найдена среди активных.", file=sys.stderr)
    return 1


def cmd_json(auth: dict, cfg: dict, mid: str) -> int:
    """Добрать JSON-отчет конкретной встречи рядом с ее MD (или в корень root)."""
    meetings_root = PROJECT_ROOT / cfg["meetings_root"]
    index = load_index(meetings_root)
    rec = index.get("meetings", {}).get(mid)
    report = api_get(auth, "/api/video/report", {"meeting_id": mid})
    if rec and rec.get("file"):
        out = (PROJECT_ROOT / rec["file"]).with_suffix(".json")
    else:
        out = meetings_root / f"{mid}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"OK: JSON -> {out.relative_to(PROJECT_ROOT)}")
    return 0


USAGE = """mymeet-snapshot - забор расшифровок встреч из mymeet.ai.

Основной режим - точечно по id (id виден в URL встречи на mymeet):
    --pull <ID|URL>   скачать одну встречу и положить по правилам проекта
    --json <ID|URL>   добрать JSON конкретной встречи

Прочее:
    --list            dry-run: что нашлось бы и куда легло (без скачивания)
    --seed            baseline: пометить текущие встречи виденными, не качая
    --all             забрать ВСЕ новые проектные встречи разом (балк; имена сырые)

Без аргументов балк не запускается намеренно (аккаунт mymeet общий на проекты).
"""


def main(argv: list[str]) -> int:
    list_only = "--list" in argv
    all_mode = "--all" in argv
    auth = load_auth()
    cfg = load_project_config()

    if "--seed" in argv:
        return cmd_seed(auth, cfg)

    if "--pull" in argv:
        i = argv.index("--pull")
        if i + 1 >= len(argv):
            sys.stderr.write("Укажи meeting_id или URL встречи: --pull <ID|URL>\n")
            return 2
        return cmd_pull(auth, cfg, extract_meeting_id(argv[i + 1]))

    if "--json" in argv:
        i = argv.index("--json")
        if i + 1 >= len(argv):
            sys.stderr.write("Укажи meeting_id или URL встречи: --json <ID|URL>\n")
            return 2
        return cmd_json(auth, cfg, extract_meeting_id(argv[i + 1]))

    if not (list_only or all_mode):
        sys.stdout.write(USAGE)
        return 0

    meetings_root = PROJECT_ROOT / cfg["meetings_root"]
    index = load_index(meetings_root)
    rules = cfg["rules"]

    pulled: list[str] = []
    review: list[str] = []
    skipped_known = 0

    for m in iter_meetings(auth):
        mid = pick(m, ID_KEYS)
        if not mid:
            continue
        mid = str(mid)
        title = pick(m, TITLE_KEYS) or ""
        status = (pick(m, STATUS_KEYS) or "").lower()
        dt = parse_date(m)

        if mid in index.get("meetings", {}):
            skipped_known += 1
            continue
        if status and status != "processed":
            continue  # еще обрабатывается / упала - вернемся на след. прогоне

        rule = match_rule(title, rules)
        date_str = dt.date().isoformat() if dt else "дата?"

        if rule is None:
            review.append(f"{mid} | {date_str} | {title}")
            continue
        if dt is None:
            # под правило подошла, но без даты не разложить - в review с пометкой
            review.append(f"{mid} | дата? | {title}  (rule={rule.get('dest')}, нет даты)")
            continue

        if list_only:
            dest_sub = build_dest(rule["dest"], dt)
            dest_file = target_file(meetings_root, dest_sub, dt, mid, index)
            rel = str(dest_file.relative_to(PROJECT_ROOT))
            pulled.append(f"{date_str} | {title}  ->  {rel}")
            continue

        try:
            rel = place_meeting(auth, cfg, meetings_root, index, m)
        except Exception as exc:
            print(f"!! {mid} ({title}): {exc}", file=sys.stderr)
            continue
        pulled.append(f"{date_str} | {title}  ->  {rel}")

    # review-файл
    if review and not list_only:
        review_path = PROJECT_ROOT / cfg["review_file"]
        review_path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# Непроектные / нераспознанные встречи mymeet (под правило не подошли)\n"
            "# Формат: meeting_id | дата | title\n"
            "# Проектную - добавь паттерн в .mymeet-snapshot.json или дерни вручную;\n"
            "# чужую - игнорируй. Обработанные строки удаляй.\n\n"
        )
        existing = ""
        if review_path.exists():
            existing = review_path.read_text(encoding="utf-8")
        known_ids = {ln.split("|")[0].strip() for ln in existing.splitlines() if "|" in ln}
        new_lines = [ln for ln in review if ln.split("|")[0].strip() not in known_ids]
        if new_lines:
            body = existing if existing else header
            review_path.write_text(body.rstrip() + "\n" + "\n".join(new_lines) + "\n",
                                   encoding="utf-8")

    if not list_only:
        meetings_root.mkdir(parents=True, exist_ok=True)
        save_index(meetings_root, index)

    # отчет
    tag = "[--list, без скачивания] " if list_only else ""
    print(f"\n{tag}новых проектных встреч: {len(pulled)}")
    for line in pulled:
        print(f"   + {line}")
    if review:
        print(f"\nне подошли под правила (в review): {len(review)}")
        for line in review[:20]:
            print(f"   ? {line}")
        if len(review) > 20:
            print(f"   ... и еще {len(review) - 20}")
    print(f"\nуже было (пропущено): {skipped_known}")
    if not list_only and review:
        print(f"review-файл: {cfg['review_file']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
