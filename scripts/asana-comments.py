#!/usr/bin/env python3
"""Чтение комментариев задачи Asana с постраничностью.

Зачем: поштучный запрос задачи отдает ленту урезанной и с САМОГО СТАРОГО конца
(у снятого в августе 2026 Asana MCP это был жесткий максимум в 50 комментариев),
поэтому у длинной задачи свежие комментарии недостижимы - а нужны как раз они.
Хуже того, выглядит это не как обрезка, а как отсутствие комментариев.
REST-эндпоинт /tasks/{gid}/stories умеет пагинацию, ее тут и разматываем.

Токен (Personal Access Token, https://app.asana.com/0/my-apps) берется из:
  1. переменной окружения ASANA_TOKEN;
  2. файла ~/.config/asana/auth.json вида {"token": "..."} (права 600).
В репозиторий и в заметки токен не кладем.

Примеры:
  python3 scripts/asana-comments.py 1215968010156987            # последние 10
  python3 scripts/asana-comments.py <gid> --last 30
  python3 scripts/asana-comments.py <gid> --all --system        # вся история + системные события
  python3 scripts/asana-comments.py <gid> --since 2026-07-20
"""

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Сборка python.org на macOS не видит системные CA - без certifi любой https
# падает с CERTIFICATE_VERIFY_FAILED. certifi в окружении уже есть (телетон).
try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

API = "https://app.asana.com/api/1.0"
AUTH_PATH = Path.home() / ".config" / "asana" / "auth.json"
FIELDS = "created_at,created_by.name,text,resource_subtype,type"


def load_token() -> str:
    token = os.environ.get("ASANA_TOKEN", "").strip()
    if token:
        return token

    if AUTH_PATH.exists():
        try:
            token = str(json.loads(AUTH_PATH.read_text(encoding="utf-8")).get("token", "")).strip()
        except (json.JSONDecodeError, OSError) as e:
            sys.exit(f"Не читается {AUTH_PATH}: {e}")
        if token:
            return token

    sys.exit(
        f"Нет токена Asana. Создай Personal Access Token на https://app.asana.com/0/my-apps и положи так:\n"
        f"  mkdir -p {AUTH_PATH.parent} && chmod 700 {AUTH_PATH.parent}\n"
        f'  printf \'{{"token": "ТОКЕН"}}\\n\' > {AUTH_PATH} && chmod 600 {AUTH_PATH}\n'
        f"либо экспортируй ASANA_TOKEN."
    )


def fetch_stories(task_gid: str, token: str) -> list[dict]:
    """Все stories задачи, разматывая next_page. Asana отдает от старых к новым."""
    stories: list[dict] = []
    url = f"{API}/tasks/{task_gid}/stories?" + urllib.parse.urlencode({"limit": 100, "opt_fields": FIELDS})

    while url:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as e:
            # Тело ошибки Asana информативно (нет доступа / неверный gid), но токен в него не попадает.
            sys.exit(f"Asana вернула {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
        except urllib.error.URLError as e:
            sys.exit(f"Сеть недоступна: {e.reason}")

        stories.extend(payload.get("data", []))
        nxt = payload.get("next_page") or {}
        url = nxt.get("uri")

    return stories


def select_stories(
    stories: list[dict], *, system: bool, since: str | None, last: int, all_: bool
) -> tuple[list[dict], int]:
    """Отбор и срез ленты. Возвращает (что показать, сколько всего после фильтров).

    Вынесено из main отдельной функцией: это единственная логика скрипта,
    проверяемая без сети, и именно в срезе жила ошибка - stories[-last:] при
    last=0 давал ВЕСЬ список (в Python -0 == 0), то есть "ноль последних"
    молча превращалось во "все".
    """
    if not system:
        stories = [s for s in stories if s.get("resource_subtype") == "comment_added"]
    if since:
        stories = [s for s in stories if s.get("created_at", "") >= since]
    total = len(stories)
    if all_:
        return stories, total
    return (stories[-last:] if last > 0 else []), total


def main() -> int:
    p = argparse.ArgumentParser(description="Комментарии задачи Asana (с постраничностью)")
    p.add_argument("task_gid", help="gid задачи (последнее число в URL задачи)")
    p.add_argument("--last", type=int, default=10, help="сколько последних показать (по умолчанию 10)")
    p.add_argument("--all", action="store_true", help="показать все, игнорируя --last")
    p.add_argument("--system", action="store_true", help="включить системные события, не только комментарии")
    p.add_argument("--since", metavar="YYYY-MM-DD", help="только начиная с этой даты")
    args = p.parse_args()

    stories, total = select_stories(
        fetch_stories(args.task_gid, load_token()),
        system=args.system,
        since=args.since,
        last=args.last,
        all_=args.all,
    )

    print(f"# задача {args.task_gid}: показано {len(stories)} из {total}\n")
    for s in stories:
        who = (s.get("created_by") or {}).get("name", "?")
        when = s.get("created_at", "")[:16].replace("T", " ")
        kind = "" if s.get("resource_subtype") == "comment_added" else f" [{s.get('resource_subtype')}]"
        print(f"--- {when} | {who}{kind}")
        print((s.get("text") or "").strip(), "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
