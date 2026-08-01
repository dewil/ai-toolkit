#!/usr/bin/env python3
"""Бэкап и восстановление куки браузерной сессии через CDP.

Зачем: сессии сайтов, которые ротируют токен при активности (LinkedIn), теряются,
если Chrome убит жестко и не успел сбросить куки на диск. Тогда нужен повторный
ручной логин с GUI - а он на удаленной машине стоит дорого. Скрипт снимает куки,
пока сессия жива, и заливает обратно после потери, без участия человека.

Работает по Chrome DevTools Protocol на голых сокетах (websocket RFC 6455),
внешних зависимостей нет.

Использование:
    python3 scripts/chrome-cookies.py dump --domain linkedin.com
    python3 scripts/chrome-cookies.py dump --domain linkedin.com --out ~/.config/browser-sessions/linkedin.json
    python3 scripts/chrome-cookies.py restore --in ~/.config/browser-sessions/linkedin.json
    python3 scripts/chrome-cookies.py list

Порт CDP: 9222 по умолчанию (для машины пользователя через обратный SSH-туннель -
тот же порт, см. скилл agent-browser), меняется флагом --port.

ФАЙЛ С КУКАМИ - СЕКРЕТ: это действующая сессия, эквивалент пароля. Кладется вне
репозитория и вне синкаемых папок, права 600 выставляются скриптом (см.
rules/secrets-handling.md). В чат, коммиты и артефакты содержимое не выводится.

Ограничения round-trip: куки с opaque partitionKey не восстанавливаются вовсе
(Storage.setCookies принимает только сериализуемый ключ, а залив такую куку без
раздела, мы расширили бы ее область) - dump предупреждает, restore пропускает.
restore отказывается лить в браузер с другим каталогом профиля, чем у источника
дампа, - снимается флагом --force.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import re
import socket
import urllib.request

DEFAULT_STORE = pathlib.Path.home() / ".config" / "browser-sessions"


def _ws_connect(url: str, timeout: float = 30.0) -> socket.socket:
    m = re.match(r"ws://([^:/]+):(\d+)(/.*)", url)
    if not m:
        raise RuntimeError(f"неожиданный ws-url: {url}")
    host, port, path = m.group(1), int(m.group(2)), m.group(3)
    s = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall(
        (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
    )
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = s.recv(4096)
        if not chunk:
            raise RuntimeError("ws handshake: соединение закрыто")
        resp += chunk
    if b" 101 " not in resp.split(b"\r\n", 1)[0]:
        raise RuntimeError("ws handshake: upgrade отклонен")
    return s


def _ws_send(s: socket.socket, payload: bytes, opcode: int = 0x1) -> None:
    mask = os.urandom(4)
    ln = len(payload)
    hdr = bytes([0x80 | opcode])
    if ln < 126:
        hdr += bytes([0x80 | ln])
    elif ln < 65536:
        hdr += bytes([0x80 | 126]) + ln.to_bytes(2, "big")
    else:
        hdr += bytes([0x80 | 127]) + ln.to_bytes(8, "big")
    s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def _ws_recv_exact(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(min(65536, n - len(buf)))
        if not chunk:
            raise RuntimeError("ws: соединение закрыто")
        buf += chunk
    return buf


def _ws_recv_msg(s: socket.socket) -> bytes:
    data = b""
    while True:
        h = _ws_recv_exact(s, 2)
        fin, opcode = h[0] & 0x80, h[0] & 0x0F
        ln = h[1] & 0x7F
        if ln == 126:
            ln = int.from_bytes(_ws_recv_exact(s, 2), "big")
        elif ln == 127:
            ln = int.from_bytes(_ws_recv_exact(s, 8), "big")
        if h[1] & 0x80:
            mask = _ws_recv_exact(s, 4)
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(_ws_recv_exact(s, ln)))
        else:
            payload = _ws_recv_exact(s, ln)
        if opcode == 0x9:
            _ws_send(s, payload, opcode=0xA)
            continue
        if opcode == 0xA:
            # unsolicited Pong разрешен RFC 6455 и приходить может в любой
            # момент, в том числе между фрагментами. Без этой ветки он попадал
            # бы в данные и ронял json.loads (и обрывал сборку фрагментов).
            continue
        if opcode == 0x8:
            raise RuntimeError("ws: закрыто со стороны Chrome")
        data += payload
        if fin:
            return data


def _cdp(s: socket.socket, msg_id: int, method: str, params: dict | None = None) -> dict:
    _ws_send(s, json.dumps({"id": msg_id, "method": method, "params": params or {}}).encode())
    while True:
        msg = json.loads(_ws_recv_msg(s))
        if msg.get("id") == msg_id:
            if "error" in msg:
                raise RuntimeError(f"CDP {method}: {msg['error']}")
            return msg.get("result", {})


def browser_version(port: int) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=8) as r:
        return json.load(r)


MARKER_HOST = "agent-browser.invalid"
MARKER_URL = f"https://{MARKER_HOST}/"
MARKER_NAME = "agent_profile_id"


def profile_id(s: socket.socket, create: bool = False) -> str | None:
    """Стабильный идентификатор профиля - кука-маркер, которую скрипт сам кладет
    в профиль при первом dump.

    Почему не версия браузера: у нее и ложные пропуски (другой профиль той же
    версии проходит), и ложные отказы (после автообновления). Почему не
    --user-data-dir из Browser.getBrowserCommandLine: метод доступен только у
    браузера, запущенного с --enable-automation, то есть в штатной установке
    вернул бы None и сверка была бы выключена. Кука живет в самом профиле,
    переживает перезапуски и обновления и ничего не требует от запуска.
    """
    got = _cdp(s, 90, "Storage.getCookies").get("cookies", [])
    for c in got:
        if c.get("name") == MARKER_NAME and MARKER_HOST in (c.get("domain") or ""):
            return c.get("value")
    if not create:
        return None
    new_id = base64.urlsafe_b64encode(os.urandom(9)).decode()
    _cdp(s, 91, "Storage.setCookies", {"cookies": [{
        "name": MARKER_NAME, "value": new_id, "url": MARKER_URL,
        "expires": 4102444800,  # 2100-01-01, чтобы не протухла между прогонами
    }]})
    return new_id


def browser_ws(port: int) -> str:
    return browser_version(port)["webSocketDebuggerUrl"]


def domain_matches(cookie_domain: str, wanted: str) -> bool:
    """Точное совпадение домена или его поддомен. Подстрока не годится:
    `--domain linkedin.com` утаскивал бы в дамп куки evil-linkedin.com -
    лишний секрет в файле, а при restore - лишние куки в браузере."""
    d = (cookie_domain or "").lstrip(".").casefold()
    w = wanted.lstrip(".").casefold()
    return d == w or d.endswith("." + w)


def cmd_dump(args) -> int:
    version = browser_version(args.port)
    s = _ws_connect(version["webSocketDebuggerUrl"])
    try:
        cookies = _cdp(s, 1, "Storage.getCookies").get("cookies", [])
        profile = profile_id(s, create=True)
    finally:
        s.close()
    if args.domain:
        cookies = [c for c in cookies if domain_matches(c.get("domain"), args.domain)]
    # маркер профиля в дамп не кладем: он про браузер, а не про сессию сайта
    cookies = [c for c in cookies if c.get("name") != MARKER_NAME]
    if not cookies:
        print(f"куки не найдены (domain={args.domain})")
        return 1
    opaque = [c.get("name") for c in cookies if c.get("partitionKeyOpaque")]
    if opaque:
        # Storage.setCookies принимает CookieParam с сериализуемым partitionKey;
        # opaque-ключ воспроизвести нельзя, поэтому restore такие пропустит.
        print(f"внимание: {len(opaque)} куки с opaque partitionKey - при restore "
              f"будут пропущены: {', '.join(filter(None, opaque[:5]))}")
    out = pathlib.Path(args.out) if args.out else DEFAULT_STORE / f"{args.domain or 'all'}.json"
    if args.out:
        out.parent.mkdir(parents=True, exist_ok=True)
    else:
        # Права правим только у своего хранилища. У произвольного --out каталог
        # чужой: menять его режим - лезть в чужие настройки (а на /tmp еще и
        # падать). Ответственность за расположение там на вызывающем.
        out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(out.parent, 0o700)  # mode у mkdir действует только при создании
    # Секрет: файл получает 600 ДО записи содержимого. O_NOFOLLOW - чтобы
    # подсунутый симлинк не увел дамп в чужой файл; fchmod правит режим уже
    # существовавшего файла (могло остаться 644 от прежней версии).
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        # Дамп сопровождаем меткой источника: restore в чужой браузер - это
        # утечка живой сессии, и без метки его нечем отличить.
        json.dump({
            "source": {"browser": version.get("Browser"), "profile": profile},
            "cookies": cookies,
        }, fh, ensure_ascii=False)
    names = sorted({c.get("name", "") for c in cookies})
    print(f"ok: {len(cookies)} куки -> {out} (600)")
    print("имена:", ", ".join(names[:12]) + ("..." if len(names) > 12 else ""))
    return 0


def cmd_restore(args) -> int:
    src = pathlib.Path(args.inp)
    if not src.exists():
        print(f"нет файла: {src}")
        return 1
    payload = json.loads(src.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        cookies = payload.get("cookies", [])
        source = payload.get("source") or {}
    else:
        cookies, source = payload, {}  # дамп старого формата - без метки источника
    opaque = [c.get("name") for c in cookies if c.get("partitionKeyOpaque")]
    if opaque:
        # Восстановить opaque-раздел нечем, а залив такую куку без него, мы бы
        # РАСШИРИЛИ ее область (partitioned -> unpartitioned). Пропускаем.
        cookies = [c for c in cookies if not c.get("partitionKeyOpaque")]
        print(f"пропущено {len(opaque)} куки с opaque partitionKey "
              f"(восстановить раздел нечем): {', '.join(filter(None, opaque[:5]))}")
    version = browser_version(args.port)
    s = _ws_connect(version["webSocketDebuggerUrl"])
    try:
        # Порт 9222 может держать не тот браузер (чужая схема, серверный Chrome),
        # а заливка сюда - отдача живой сессии постороннему профилю. Сверяем
        # маркер профиля; неизвестен - отказываем (fail-closed): "не смогли
        # проверить" не то же самое, что "проверили и совпало".
        here = profile_id(s)
        want = source.get("profile")
        if want != here or not want:
            why = (f"дамп снят с профиля {want}, а на порту {args.port} - {here}"
                   if want and here else
                   "профиль источника или текущий неизвестен "
                   "(дамп старого формата либо чужой браузер)")
            print(f"отказ: {why}. Тот ли это браузер? Осознанно - повторите с --force")
            if not args.force:
                return 2
        _cdp(s, 1, "Storage.setCookies", {"cookies": cookies})
    finally:
        s.close()
    print(f"ok: восстановлено {len(cookies)} куки из {src}")
    return 0


def cmd_list(args) -> int:
    if not DEFAULT_STORE.exists():
        print(f"хранилище пусто: {DEFAULT_STORE}")
        return 0
    for f in sorted(DEFAULT_STORE.glob("*.json")):
        st = f.stat()
        print(f"{f.name}\t{st.st_size} B\tmode {oct(st.st_mode & 0o777)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Бэкап и восстановление куки через CDP")
    ap.add_argument("--port", type=int, default=9222, help="порт CDP (по умолчанию 9222)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("dump", help="снять куки из живого браузера")
    d.add_argument("--domain", help="фильтр по домену, например linkedin.com")
    d.add_argument("--out", help="путь файла (по умолчанию ~/.config/browser-sessions/<domain>.json)")
    d.set_defaults(func=cmd_dump)

    r = sub.add_parser("restore", help="залить куки в браузер")
    r.add_argument("--in", dest="inp", required=True, help="файл с куками")
    r.add_argument("--force", action="store_true",
                   help="залить, даже если браузер не совпал с источником дампа")
    r.set_defaults(func=cmd_restore)

    l = sub.add_parser("list", help="что лежит в хранилище")
    l.set_defaults(func=cmd_list)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
