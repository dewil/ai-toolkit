#!/usr/bin/env python3
"""Структурное решение по тексту через Jev (TypeSafe AI) на OpenRouter.

Jev не пишет текст: на вход текст и набор вопросов, на выход ответ на каждый
вопрос в заданной форме - noul (да/нет; тип так и называется), choice
(вариант из списка), score (ступень шкалы) - с вероятностями. Годится на
сортировку и предфильтр, не на разбор, не на ревью и не как гейт.

Только публичные тексты: сервис зарубежный, персональные данные и закрытый
материал сюда не отправляются.

    python3 scripts/jev-decide.py --questions <вопросы.json> --state-file <текст>
    echo "текст" | python3 scripts/jev-decide.py --questions <вопросы.json>

Ключ: переменная OPENROUTER_API_KEY или файл ~/.config/openrouter/key (права 600).
Прокси: --proxy, иначе JEV_PROXY, иначе HTTPS_PROXY окружения. В cron переменных
профиля нет - прокси передается явно, иначе с заблокированного выхода 403.
Вывод - JSON ответа сервиса как есть (stdout).

Коды возврата:
  0 - ответ получен, на каждый вопрос есть ответ;
  1 - ошибка сети, HTTP, ключа или входа;
  3 - ответ получен, но не на каждый вопрос есть пригодный ответ по схеме
      (тип, значение, вариант из критериев) или формат не распознан.
      JSON все равно печатается - смотреть глазами, результат не использовать.
Эндпоинт помечен у OpenRouter как alpha: формат может смениться без
предупреждения. Сверка идет по опубликованной схеме (api-reference, раздел
alphadecisions, сверено 25.09.2026); ключ в выводе маскируется, файл ключа с
правами шире 600 не читается.
"""
import argparse
import json
import os
import re
import stat
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

URL = "https://openrouter.ai/api/alpha/decisions"
MODEL = "~typesafe/jev-latest"
KEY_FILE = Path.home() / ".config/openrouter/key"
DEADLINE = 90          # общий предел вызова, с: сокетный timeout ограничивает одну операцию, не весь ответ
_KEY = ""              # для маскировки в выводе
_PROXY = ""            # адрес прокси тоже маскируется: в нем бывают логин и пароль


def mask(text: str) -> str:
    """Ключ в любом выводе заменяется: шлюз может отразить заголовок в диагностике."""
    for secret in (_KEY, _PROXY):
        if secret:
            for form in {secret, json.dumps(secret)[1:-1]}:   # как есть и в JSON-экранировании
                text = text.replace(form, "***")
    return text


def mask_err(text: str) -> str:
    """Для диагностики (stderr): плюс учетные данные в любом URL и Basic/Bearer-токены.

    Шаблонная маскировка только здесь: на успешном ответе она портила бы данные
    ("basic knowledge" в варианте choice). Ответ сервиса маскируется по точным
    значениям ключа и прокси, его содержимое не меняется.
    """
    text = mask(text)
    text = re.sub(r"([a-zA-Z][a-zA-Z0-9+.-]*:/{0,2})[^/@\s'\"]+@", r"\1***@", text)
    return re.sub(r"(?i)(basic|bearer)\s+[A-Za-z0-9+/=._-]{8,}", r"\1 ***", text)


def api_key() -> str:
    global _KEY
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key and KEY_FILE.exists():
        mode = KEY_FILE.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            sys.exit(f"{KEY_FILE}: права {oct(mode & 0o777)}, нужны 600 - ключ открыт другим пользователям (chmod 600)")
        key = KEY_FILE.read_text().strip()
    if not key:
        sys.exit(f"не задан ключ: OPENROUTER_API_KEY или {KEY_FILE}")
    _KEY = key
    return key


def resolve_proxy(cli: str | None) -> tuple[str, str]:
    """Прокси и откуда он взят: --proxy > JEV_PROXY > HTTPS_PROXY окружения > без прокси.

    Явный источник нужен для cron: там нет переменных профиля пользователя, и
    без прокси сервис за Cloudflare отвечает 403 на каждый вызов, тогда как
    интерактивная проверка зеленая. Адрес в выводе не печатается - в нем бывают
    логин и пароль; печатается только источник.
    """
    for value, src in ((cli, "--proxy"), (os.environ.get("JEV_PROXY"), "JEV_PROXY"),
                       (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"), "HTTPS_PROXY")):
        if value and value.strip():
            return value.strip(), src
    return "", "не задан"


def missing_answers(response, questions: dict) -> list[str] | None:
    """Вопросы без пригодного ответа; None - формат ответа не распознан.

    Схема OpenRouter (api-reference, alphadecisions): answers - объект, ключи -
    имена вопросов, значение - {"type": ..., <type>: значение, ...}: noul - число
    0..1, choice - строка из критериев вопроса, score - число. Ответ другого типа,
    без значения или с вариантом, которого в вопросе нет, - не ответ.
    """
    if not isinstance(response, dict) or not isinstance(response.get("answers"), dict):
        return None
    if response.get("error"):
        return None    # ошибка провайдера внутри HTTP 200: ответы при ней не доверенные
    answers, bad = response["answers"], []
    for name, q in questions.items():
        a = answers.get(name)
        qtype = q.get("type") if isinstance(q, dict) else None
        if not isinstance(a, dict) or a.get("type") != qtype:
            bad.append(name)
            continue
        v = a.get(qtype)
        if qtype == "noul":
            ok = isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1
        elif qtype == "choice":
            crit = q.get("criteria")
            ok = isinstance(v, str) and isinstance(crit, dict) and v in crit
        elif qtype == "score":
            levels = q.get("criteria")
            top = len(levels) - 1 if isinstance(levels, (list, dict)) and levels else None
            ok = (isinstance(v, (int, float)) and not isinstance(v, bool)
                  and v >= 0 and (top is None or v <= top))
        else:
            ok = False
        if not ok:
            bad.append(name)
    return bad


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--questions", required=True, help="JSON с вопросами (поле questions запроса)")
    ap.add_argument("--state-file", help="файл с текстом; без него текст читается из stdin")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--proxy", help="прокси для запроса (иначе JEV_PROXY, иначе HTTPS_PROXY окружения)")
    a = ap.parse_args()

    try:
        questions = json.loads(Path(a.questions).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"вопросы не прочитаны: {e}")
    if not isinstance(questions, dict) or not questions:
        sys.exit("вопросы: ожидается непустой JSON-объект {имя: {type, instructions, criteria}}")
    state = Path(a.state_file).read_text(encoding="utf-8") if a.state_file else sys.stdin.read()
    if not state.strip():
        sys.exit("пустой текст на входе")

    body = json.dumps({"model": a.model, "state": state, "questions": questions}).encode()
    req = urllib.request.Request(URL, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key()}",
    })
    # Общий предел: сокетный timeout ограничивает одну операцию, а медленный
    # ответ малыми порциями держал бы вызов сколько угодно. Запрос - в потоке,
    # по истечении DEADLINE выходим, что бы там ни висело (поток daemon).
    result: dict = {}

    proxy, proxy_src = resolve_proxy(a.proxy)
    global _PROXY
    _PROXY = proxy
    if proxy:
        # urllib сверяет хост с NO_PROXY и при совпадении идет мимо даже явного
        # прокси - тогда 403 назвал бы источником прокси то, что не использовалось
        for var in ("NO_PROXY", "no_proxy"):
            os.environ.pop(var, None)
    opener = (urllib.request.build_opener(urllib.request.ProxyHandler({"https": proxy, "http": proxy}))
              if proxy else urllib.request.build_opener(urllib.request.ProxyHandler({})))

    def fetch():
        try:
            with opener.open(req, timeout=30) as r:
                result["raw"] = r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code == 403 and body.lstrip()[:1] == "<":
                result["err"] = (f"HTTP 403 с html-страницей вместо ответа API: отказал шлюз сервиса "
                                 f"(обычно - заблокирован выход машины) или сам прокси; прокси: {proxy_src}. "
                                 f"Без прокси - задайте --proxy или JEV_PROXY; с прокси - проверьте его")
            else:
                result["err"] = f"HTTP {e.code}: {body[:500]}"
        except Exception as e:            # сеть, таймаут, TLS - все в код 1
            result["err"] = f"сеть: {getattr(e, 'reason', e)}"

    worker = threading.Thread(target=fetch, daemon=True)
    worker.start()
    worker.join(DEADLINE)
    if worker.is_alive():
        print(f"сеть: ответ не уложился в {DEADLINE} с", file=sys.stderr)
        sys.exit(1)
    if "err" in result:
        print(mask_err(result["err"]), file=sys.stderr)
        sys.exit(1)
    raw = result["raw"]
    try:
        response = json.loads(raw)
    except json.JSONDecodeError:
        print(mask(raw))
        print("ответ сервиса - не JSON", file=sys.stderr)
        sys.exit(3)

    print(mask(json.dumps(response, ensure_ascii=False, indent=2)))
    miss = missing_answers(response, questions)
    if miss is None:
        print("формат ответа не распознан (нет answers или есть error) - результат не использовать", file=sys.stderr)
        sys.exit(3)
    if miss:
        print(mask_err(f"нет пригодного ответа на: {', '.join(miss)} - результат не использовать"), file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
