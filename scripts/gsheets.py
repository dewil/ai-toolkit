#!/usr/bin/env python3
"""Чтение и запись Google Sheets напрямую через REST API, без MCP.

Зачем: MCP-сервер google-workspace периодически теряет регистрацию инструментов
в сессии (сервер жив, инструменты недоступны до перезапуска клиента). Работа с
таблицей от этого вставать не должна - здесь тот же доступ, но по прямому HTTP,
на одних лишь stdlib. Годится и там, где MCP неприменим в принципе: cron,
headless-прогоны, скрипты.

Учетные данные - свои, в ~/.config/gsheets/auth.json (client_id, client_secret,
refresh_token, token_uri; права 600). Access-токен обновляется сам по
refresh_token, интерактивной авторизации не требуется.

Если своего конфига нет, скрипт разово подхватывает учетные данные из хранилища
workspace-mcp и предлагает скопировать их к себе: refresh_token привязан к
OAuth-клиенту, а не к MCP, поэтому после копирования MCP не нужен вовсе.

Использование:

    python3 scripts/gsheets.py read <SPREADSHEET_ID> "'Лист'!A1:D10"
    python3 scripts/gsheets.py read <ID> "'Лист'!A1:D10" --formulas
    echo '[["=SUM(A1:A5)", 42]]' | python3 scripts/gsheets.py write <ID> "'Лист'!B1:C1"
    python3 scripts/gsheets.py sheets <ID>

Запись идет с valueInputOption=USER_ENTERED (формулы и даты интерпретируются,
как при ручном вводе). Разделитель аргументов в формулах - тот же, что в самой
таблице (в русской локали - точка с запятой).
"""

import json
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AUTH = Path.home() / ".config" / "gsheets" / "auth.json"
MCP_CRED_DIR = Path.home() / ".google_workspace_mcp" / "credentials"
API = "https://sheets.googleapis.com/v4/spreadsheets"

REQUIRED = ("client_id", "client_secret", "refresh_token", "token_uri")


def load_creds() -> dict:
    """Свой конфиг; при его отсутствии - разовый фолбэк на хранилище workspace-mcp."""
    if AUTH.exists():
        # Файл с refresh_token равносилен паролю (rules/secrets-handling.md):
        # доступный группе или всем, он утекает вместе с домашним каталогом.
        mode = AUTH.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            sys.stderr.write(f"внимание: {AUTH} доступен не только владельцу "
                             f"({oct(mode & 0o777)}); поставь права 600\n")
        try:
            creds = json.loads(AUTH.read_text())
        except ValueError as exc:
            sys.exit(f"{AUTH} - не валидный JSON: {exc}")
        missing = [k for k in REQUIRED if not creds.get(k)]
        if missing:
            sys.exit(f"в {AUTH} не хватает полей: {', '.join(missing)}")
        return creds

    files = sorted(p for p in MCP_CRED_DIR.glob("*.json")
                   if p.name != "oauth_states.json") if MCP_CRED_DIR.is_dir() else []
    if not files:
        sys.exit(
            f"нет учетных данных: {AUTH}\n"
            "заведи файл с полями client_id, client_secret, refresh_token, token_uri "
            "(права 600); взять их можно из любого уже авторизованного OAuth-клиента "
            "с областью spreadsheets"
        )
    if len(files) > 1:
        sys.exit(f"в {MCP_CRED_DIR} несколько аккаунтов - скопируй нужный в {AUTH}")
    creds = json.loads(files[0].read_text())
    sys.stderr.write(
        f"внимание: учетные данные взяты из хранилища workspace-mcp ({files[0].name}).\n"
        f"скопируй их в {AUTH} (поля {', '.join(REQUIRED)}, права 600) - "
        "тогда скрипт перестанет зависеть от MCP совсем.\n"
    )
    return creds


def access_token(creds: dict) -> str:
    """Меняет refresh_token на свежий access_token.

    Access-токен живет час и не кешируется: обновление стоит одного запроса,
    а протухший токен стоит непонятной 401 посреди работы.
    """
    body = urllib.parse.urlencode({
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(creds["token_uri"], data=body)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)["access_token"]
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf8", "replace")[:300]
        # invalid_grant - самый вероятный отказ: токен отозвали или он протух.
        # Лечится повторным копированием refresh_token, а не кодом.
        hint = ("\nrefresh_token отозван или протух - получи новый и обнови "
                f"{AUTH}") if "invalid_grant" in detail else ""
        sys.exit(f"не удалось обновить access_token (HTTP {e.code}): {detail}{hint}")
    except urllib.error.URLError as e:
        sys.exit(f"не достучались до {creds['token_uri']}: {e.reason}")
    except (KeyError, ValueError) as e:
        sys.exit(f"ответ сервера токенов не содержит access_token: {e}")


def api(token: str, path: str, params: dict | None = None,
        method: str = "GET", payload: dict | None = None):
    url = f"{API}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf8", "replace")[:500]
        sys.exit(f"HTTP {e.code} от Sheets API: {detail}")
    except urllib.error.URLError as e:
        # Нет сети, DNS, прокси - без этого пользователь видел бы трейсбек.
        sys.exit(f"не достучались до Sheets API: {e.reason}")


def cmd_read(token: str, sid: str, rng: str, formulas: bool) -> int:
    params = {"valueRenderOption": "FORMULA" if formulas else "UNFORMATTED_VALUE"}
    out = api(token, f"{sid}/values/{urllib.parse.quote(rng)}", params)
    for row in out.get("values", []):
        print("\t".join("" if c is None else str(c) for c in row))
    return 0


def cmd_write(token: str, sid: str, rng: str) -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        sys.exit("на stdin пусто: ожидается JSON-массив строк, например [[1,2],[3,4]]")
    try:
        values = json.loads(raw)
    except ValueError as exc:
        sys.exit(f"на stdin не JSON: {exc}")
    if not isinstance(values, list) or not all(isinstance(r, list) for r in values):
        sys.exit("ожидается массив строк: [[\"a\", 1], [\"b\", 2]]")
    out = api(token, f"{sid}/values/{urllib.parse.quote(rng)}",
              {"valueInputOption": "USER_ENTERED"}, "PUT", {"values": values})
    print(f"OK: обновлено ячеек {out.get('updatedCells')} в {out.get('updatedRange')}")
    return 0


def cmd_sheets(token: str, sid: str) -> int:
    out = api(token, sid, {"fields": "sheets.properties"})
    for s in out.get("sheets", []):
        p = s["properties"]
        grid = p.get("gridProperties", {})
        print(f"{p['sheetId']:>12}  {p['title']}  ({grid.get('rowCount')}x{grid.get('columnCount')})")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1
    cmd = argv[1]
    # Аргументы проверяем ДО обращения к сети: на опечатке в команде
    # пользователь должен видеть usage, а не ошибку авторизации.
    needed = {"read": 4, "write": 4, "sheets": 3}
    if cmd not in needed or len(argv) < needed[cmd]:
        print(__doc__)
        return 1
    token = access_token(load_creds())
    if cmd == "read":
        return cmd_read(token, argv[2], argv[3], "--formulas" in argv)
    if cmd == "write":
        return cmd_write(token, argv[2], argv[3])
    return cmd_sheets(token, argv[2])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
