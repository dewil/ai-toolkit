#!/usr/bin/env python3
"""Оформление upstream-кандидата в канон: одна команда вместо двух записей.

Зачем: бриф должен лечь в ДВА места - в очередь канона `~/.claude/canon-inbox/`
(транспорт до клона) и в `toolkit-log/upstream-pending/` проекта (история и
источник для синка). Пока это были два действия агента, вторым забывали именно
доставку - за три недели так потерялось три брифа из девяти. Правило про это
было написано подробно и не помогло: инструкция "не забудь сделать еще шаг"
отказывает независимо от того, насколько хорошо она написана.

Отсюда две вещи, ради которых скрипт и существует:

1. **Одно действие вместо двух.** Половина работы тут невозможна: обе записи
   делает один вызов.
2. **Очередь пишется ПЕРВОЙ.** Порядок не косметический - он определяет, что
   теряется при сбое. Раньше терялась находка (навсегда), теперь - локальная
   копия в проекте, которую восстановит следующий `check --redeliver`.

Подкоманды:
  deliver - оформить бриф: очередь, затем проект
  check   - сверка в ОБЕ стороны плюс сравнение содержимого. --redeliver
            доставляет застрявшее в очередь (направление, где имя брифа
            известно точно); --restore-local восстанавливает проектную копию
            по файлу очереди (там имя выводится из имени файла, разбор
            неоднозначен - поэтому отдельный флаг). Расхождение содержимого
            не чинится автоматически: какая версия верна, решает человек

Примеры:
  python3 scripts/canon-brief.py deliver --name md-docx-numbering < бриф.md
  python3 scripts/canon-brief.py deliver --name X --from черновик.md
  python3 scripts/canon-brief.py check
  python3 scripts/canon-brief.py check --redeliver

Адрес очереди фиксированный (`rules/upstream-inbox.md`); переменная
CANON_INBOX существует для тестов и нештатных раскладок, а не для того, чтобы
подбирать место по обстоятельствам.

Коды возврата:
  0 - все записи сделаны (или check не нашел расхождений)
  1 - ошибка вызова (нет имени, пустой бриф, недоступен проект)
  2 - НЕ записана очередь: бриф не доставлен, находка под угрозой
  3 - очередь записана, проектная копия нет: находка в безопасности
  4 - check нашел расхождение между проектом и очередью (без --redeliver)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import unicodedata
from datetime import date, datetime
from pathlib import Path

INBOX = Path(os.environ.get("CANON_INBOX") or (Path.home() / ".claude" / "canon-inbox"))
PENDING = Path("toolkit-log") / "upstream-pending"
TERMINAL = ("applied", "rejected")

# Транслитерация для папок без ASCII-токена в имени. Таблица маленькая
# намеренно: слаг нужен стабильный и читаемый, а не филологически точный.
TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y",
    "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}-")
DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _umask() -> int:
    cur = os.umask(0)
    os.umask(cur)
    return cur


def safe_line(text: str) -> str:
    """Имя файла для построчного вывода: управляющие символы обезврежены."""
    return "".join(ch if ch.isprintable() else "?" for ch in text)


def die(code: int, msg: str) -> None:
    sys.stderr.write(msg.rstrip("\n") + "\n")
    sys.exit(code)


def slugify(text: str) -> tuple[str, bool]:
    """Имя папки -> слаг. Второе значение - была ли транслитерация.

    Слаг обязан быть стабильным между прогонами: он опознает кандидата в
    очереди, и уехавший слаг означает второй файл про ту же находку.
    """
    lowered = unicodedata.normalize("NFC", text).lower()
    out = []
    translit = False
    for ch in lowered:
        if ch in TRANSLIT:
            out.append(TRANSLIT[ch])
            translit = True
        elif ch.isascii() and (ch.isalnum()):
            out.append(ch)
        else:
            out.append("-")
    slug = re.sub(r"-+", "-", "".join(out)).strip("-")
    return slug, translit


SLUG_FILE = Path("toolkit-log") / ".canon-slug"


def remember_slug(root: Path, slug: str) -> None:
    """Заданный вручную слаг запоминается в проекте.

    Иначе `deliver --slug X` и следующий `check` без флага работали с разными
    слагами: сверка объявляла собственный бриф недоставленным и клала дубль.
    """
    try:
        write_atomic(root / SLUG_FILE, slug + "\n")
    except OSError as e:
        sys.stderr.write(f"внимание: слаг не запомнен ({e}) - следующей команде "
                         f"передай --slug {slug}\n")


def read_saved_slug(root: Path) -> str:
    """Слаг из `toolkit-log/.canon-slug`, проверенный.

    Файл лежит в проекте и правится чем угодно, а слаг идет прямо в путь -
    непроверенное значение вида `x/../../escaped` уводило запись за пределы
    очереди. Поэтому оно прогоняется через тот же slugify и принимается, только
    если совпало с собой: любое расхождение значит, что файл трогали руками.
    """
    try:
        raw = (root / SLUG_FILE).read_bytes().decode("utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    if not raw:
        return ""
    clean, _ = slugify(raw)
    if clean != raw:
        sys.stderr.write(f"внимание: {SLUG_FILE} содержит {raw!r} - это не слаг; "
                         "файл игнорируется, слаг выводится из имени папки\n")
        return ""
    return clean


def project_slug(root: Path, override: str | None) -> str:
    if override is None:
        saved = read_saved_slug(root)
        if saved:
            return saved
    if override:
        slug, _ = slugify(override)
        if not slug:
            die(1, f"--slug {override!r} не дает ASCII-слага")
        return slug
    slug, translit = slugify(root.resolve().name)
    if not slug:
        die(1, f"из имени папки {root.resolve().name!r} не выводится слаг - задай --slug")
    if translit:
        # Молчать нельзя: слаг войдет в имя файла очереди, и владелец должен
        # знать, под каким именем кандидат будет опознаваться дальше
        sys.stderr.write(f"внимание: в имени папки нет ASCII-токена, слаг получен "
                         f"транслитерацией: {slug}\n")
    return slug


def check_name(name: str) -> str:
    """Имя брифа - один сегмент пути, без разделителей и без '..'."""
    name = name.strip()
    if name.endswith(".md"):
        name = name[:-3]
    if not SAFE_NAME.match(name) or ".." in name:
        die(1, f"имя брифа {name!r} не годится: латиница, цифры, дефис, точка и "
               "подчеркивание, первый символ - буква или цифра, без разделителей пути")
    return name


def write_atomic(path: Path, data: str) -> None:
    """Запись через временный файл в той же папке плюс rename.

    Оборванная на середине запись дала бы обрезанный бриф, который выглядит
    доставленным: это тот же класс, что молчаливый провал доставки.

    Имя временного файла уникальное, а не `<цель>.tmp`: предсказуемое имя
    ломало два параллельных прогона (второй получал ENOENT на уже
    переименованном файле) и позволяло подменить цель заранее подложенным
    симлинком. При ошибке временный файл убирается - иначе он остается
    мусором рядом с очередью.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".cb-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        # mkstemp дает 0600; перезапись не должна молча отбирать у файла права,
        # которые у него были, а новый файл получает обычные для umask
        try:
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        except OSError:
            os.chmod(tmp, 0o666 & ~_umask())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def split_queue_name(fname: str) -> tuple[str, str] | None:
    """`<YYYY-MM-DD>-<slug>-<имя>.md` -> (слаг, имя), иначе None.

    Граница слаг/имя неоднозначна по определению (оба содержат дефисы),
    поэтому разбор всегда идет ОТ известного слага - см. matches_candidate.
    Здесь только снимается дата и суффикс.
    """
    if not DATE_PREFIX.match(fname) or not fname.endswith(".md"):
        return None
    try:
        datetime.strptime(fname[:10], "%Y-%m-%d")
    except ValueError:
        # `2026-99-99-...` формат проходит, датой не является: принимать такой
        # файл за доставку значит считать доставленным то, чего мы не писали
        return None
    return "", fname[11:-3]


def matches_candidate(fname: str, slug: str, name: str) -> bool:
    """Тот ли это кандидат. Сравнение структурное, а не по хвосту имени.

    Глоб `*-<slug>-<name>.md` совпадал, когда наш слаг оказывался суффиксом
    чужого: проект `alp` подхватывал файлы проекта `foo-alp` - и удалял их
    как свою прежнюю версию. Разбор от известного слага такой неоднозначности
    не допускает.
    """
    parsed = split_queue_name(fname)
    if parsed is None:
        return False
    rest = parsed[1]
    return rest == f"{slug}-{name}"


def queue_matches(slug: str, name: str) -> list[Path]:
    """Файлы очереди про этого же кандидата, под любой датой.

    Дата в имени меняется при переоформлении, кандидат - нет.
    """
    if not INBOX.is_dir():
        return []
    return sorted(p for p in INBOX.glob("*.md")
                  if p.is_file() and matches_candidate(p.name, slug, name))


def terminal_matches(slug: str, name: str) -> list[Path]:
    """Тот же кандидат в applied/ или rejected/ - то есть уже разобранный."""
    found = []
    for sub in TERMINAL:
        d = INBOX / sub
        if d.is_dir():
            found += [p for p in d.glob("*.md")
                      if p.is_file() and matches_candidate(p.name, slug, name)]
    return sorted(found)


def compare_copies(slug: str, root: Path) -> list[str]:
    """Пары, где обе копии есть, но содержимое разное.

    Без этого сверка молчала при `pending v1 / queue v2`: имена совпадают,
    значит "доставлено", а версии разные навсегда. Какая из них верна - решает
    человек, поэтому автоматически не чинится.
    """
    if not INBOX.is_dir():
        return []
    diff = []
    for q in sorted(INBOX.glob("*.md")):
        parsed = split_queue_name(q.name) if q.is_file() else None
        if parsed is None or not parsed[1].startswith(slug + "-"):
            continue
        local = root / PENDING / (parsed[1][len(slug) + 1:] + ".md")
        if not local.is_file():
            continue
        try:
            if q.read_bytes() != local.read_bytes():
                diff.append(local.name)
        except OSError:
            continue
    return diff


def missing_project_copies(slug: str, root: Path) -> list[Path]:
    """Файлы очереди этого проекта, у которых нет копии в upstream-pending/.

    Обратное направление сверки. Без него код возврата 3 (очередь записана,
    проектная копия нет) было нечем починить, хотя правило обещало починку -
    то есть обещание молчало одинаково при исправном и сломанном состоянии.

    Бриф, уже переведенный проектом в терминальную папку, не воскрешается:
    отсутствие копии в pending там означает завершенный жизненный цикл, а не
    потерю.
    """
    if not INBOX.is_dir():
        return []
    out = []
    for q in sorted(INBOX.glob("*.md")):
        parsed = split_queue_name(q.name) if q.is_file() else None
        if parsed is None or not parsed[1].startswith(slug + "-"):
            continue
        name = parsed[1][len(slug) + 1:] + ".md"
        if (root / PENDING / name).exists():
            continue
        if any((root / "toolkit-log" / t / name).exists()
               for t in ("upstream-applied", "upstream-rejected")):
            continue
        out.append(q)
    return out


def deliver_one(body: str, name: str, slug: str, root: Path, stamp: str,
                dry_run: bool) -> int:
    inbox_path = INBOX / f"{stamp}-{slug}-{name}.md"
    project_path = root / PENDING / f"{name}.md"

    stale = [p for p in queue_matches(slug, name) if p != inbox_path]
    done = terminal_matches(slug, name)

    if dry_run:
        print("DRY-RUN (ничего не записано)")
        print(f"  очередь:  {inbox_path}")
        print(f"  проект:   {project_path}")
        for p in stale:
            print(f"  заменит:  {p.name} (тот же кандидат под другой датой)")
        for p in done:
            print(f"  внимание: этот кандидат уже разобран ({p.parent.name}/{p.name})")
        return 0

    # Очередь ПЕРВОЙ: при сбое теряется проектная копия, а не находка
    try:
        write_atomic(inbox_path, body)
    except OSError as e:
        die(2, f"НЕ УДАЛОСЬ записать очередь {inbox_path}: {e}\n"
               "Бриф НЕ доставлен - находка потеряется. Проектную копию не пишу, "
               "чтобы не выглядело сделанным.")
    for p in stale:
        try:
            p.unlink()
        except OSError as e:
            sys.stderr.write(f"внимание: не удалось убрать прежнюю версию {p.name}: {e}\n")
    print(f"очередь: {inbox_path}")
    for p in done:
        # Разобранный кандидат в терминальной папке - не ошибка, но повод
        # сказать: возможно, находку предлагают повторно, не зная об этом
        print(f"  внимание: кандидат с таким именем уже разобран - {p.parent.name}/{p.name}")

    try:
        write_atomic(project_path, body)
    except OSError as e:
        sys.stderr.write(f"очередь записана, а проектная копия {project_path} - нет: {e}\n"
                         "Находка в безопасности; локальную копию восстановит "
                         "canon-brief.py check --redeliver\n")
        return 3
    print(f"проект:  {project_path}")
    return 0


def check_date(value: str | None) -> str:
    """Дата идет прямо в имя файла очереди: без проверки '../..' и '/tmp/x'
    уводили запись за пределы очереди, а прогон при этом завершался успешно."""
    if value is None:
        return date.today().isoformat()
    if not DATE_ONLY.match(value):
        die(1, f"--date {value!r} - ожидается YYYY-MM-DD")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        die(1, f"--date {value!r} - не существующая дата")
    return value


def read_body(source: str) -> str:
    """Текст брифа. Не-UTF8 вход - контролируемый отказ, а не traceback."""
    try:
        if source == "-":
            data = sys.stdin.buffer.read()
        else:
            src = Path(source)
            if not src.is_file():
                die(1, f"нет файла с текстом брифа: {src}")
            data = src.read_bytes()
        body = data.decode("utf-8")
    except UnicodeDecodeError as e:
        die(1, f"текст брифа не в UTF-8: {e}")
    except OSError as e:
        die(1, f"не читается текст брифа: {e}")
    if not body.replace("\ufeff", "").strip():
        die(1, "текст брифа пуст - нечего доставлять")
    return body if body.endswith("\n") else body + "\n"


def cmd_deliver(args) -> int:
    root = Path(args.project_root).resolve()
    if not root.is_dir():
        die(1, f"нет каталога проекта: {root}")
    name = check_name(args.name)
    slug = project_slug(root, args.slug)
    stamp = check_date(args.date)
    body = read_body(args.source)
    rc = deliver_one(body, name, slug, root, stamp, args.dry_run)
    if not args.dry_run and args.slug:
        remember_slug(root, slug)
    return rc


def cmd_check(args) -> int:
    """Сверка проекта с очередью в обе стороны - бэкстоп на ручную запись.

    Живет там же, где файлы: синку и наблюдению не нужен реестр проектов, они
    просто идут по текущему проекту.
    """
    root = Path(args.project_root).resolve()
    sweep_temp()
    pending = root / PENDING
    slug = project_slug(root, args.slug)
    briefs = sorted(p for p in pending.glob("*.md") if p.is_file()) if pending.is_dir() else []
    if not briefs and not missing_project_copies(slug, root):
        print("upstream-pending пуст - проект ничего не выносил в канон"
              if not pending.is_dir() else "upstream-pending пуст")
        return 0

    stranded: list[Path] = []
    for b in briefs:
        name = b.name[:-3]
        if queue_matches(slug, name) or terminal_matches(slug, name):
            continue
        stranded.append(b)
    missing_local = missing_project_copies(slug, root)
    diverged = compare_copies(slug, root)

    if not stranded and not missing_local and not diverged:
        print(f"брифов: {len(briefs)}, все доставлены")
        return 0

    if stranded:
        print(f"брифов: {len(briefs)}, НЕ доставлено: {len(stranded)}")
        for b in stranded:
            # управляющие символы в имени подделали бы построчный протокол,
            # по которому этот вывод читает наблюдение toolkit-репо
            print(f"  - {safe_line(b.name)}")
    if missing_local:
        # Направление обратное, и имя брифа тут не известно, а выводится из
        # имени файла очереди. Разбор неоднозначен, когда чужой слаг начинается
        # с нашего (`alp` против `alp-foo`), поэтому автоматически такие файлы
        # НЕ восстанавливаются: лишний бриф в чужом проекте хуже, чем ручной шаг
        print(f"в очереди есть, а в проекте нет: {len(missing_local)}")
        for q in missing_local:
            print(f"  - {safe_line(q.name)}")
        print("  (восстановление локальной копии - только явным --restore-local:"
              " имя брифа тут выводится из имени файла очереди)")
    if diverged:
        # Молчать нельзя, но и чинить нечем: какая версия верна - решает человек
        print(f"копии разошлись по содержимому: {len(diverged)}")
        for n in diverged:
            print(f"  - {safe_line(n)}")
    if not args.redeliver and not args.restore_local:
        hints = []
        if stranded:
            hints.append("доставить застрявшее: check --redeliver")
        if missing_local:
            hints.append("вернуть копии в проект: check --restore-local")
        if diverged:
            hints.append("расхождение версий чинится руками: какая верна, "
                         "решает человек")
        for h in hints:
            print(h)
        return 4

    failed = 0
    if not args.redeliver:
        stranded = []
    if not args.restore_local:
        missing_local = []
    for b in stranded:
        # Дата - по времени файла, а не сегодняшняя: имя в очереди должно
        # говорить, когда находку нашли, иначе застрявший месяц назад бриф
        # выглядит свежим
        stamp = datetime.fromtimestamp(b.stat().st_mtime).date().isoformat()
        try:
            write_atomic(INBOX / f"{stamp}-{slug}-{b.name}",
                         b.read_bytes().decode("utf-8"))
            print(f"доставлен: {stamp}-{slug}-{safe_line(b.name)}")
        except (OSError, UnicodeDecodeError) as e:
            sys.stderr.write(f"НЕ доставлен {safe_line(b.name)}: {e}\n")
            failed += 1
    for q in missing_local:
        name = split_queue_name(q.name)[1][len(slug) + 1:] + ".md"
        try:
            write_atomic(root / PENDING / name, q.read_bytes().decode("utf-8"))
            print(f"восстановлена копия в проекте: {safe_line(name)}")
        except (OSError, UnicodeDecodeError) as e:
            sys.stderr.write(f"НЕ восстановлена копия {safe_line(name)}: {e}\n")
            failed += 1
    if diverged:
        return 4
    return 2 if failed else 0


def sweep_temp() -> None:
    """Остатки временных файлов от убитых прогонов.

    `mkstemp` дает уникальное имя, поэтому мусор не мешает работе, но копится
    рядом с очередью и выглядит как ее содержимое. Уборка тут, а не при записи:
    убитый процесс за собой убрать не может по определению.
    """
    if not INBOX.is_dir():
        return
    for t in INBOX.glob(".cb-*.tmp"):
        try:
            t.unlink()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Оформление upstream-кандидата: очередь канона + копия в проекте.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project-root", default=".", help="корень проекта (по умолчанию текущий каталог)")
    common.add_argument("--slug", help="слаг проекта; по умолчанию выводится из имени папки")

    d = sub.add_parser("deliver", parents=[common],
                       help="оформить бриф: очередь канона, затем копия в проекте")
    d.add_argument("--name", required=True, help="имя брифа без .md")
    d.add_argument("--from", dest="source", default="-",
                   help="файл с текстом брифа ('-' или пропуск - stdin)")
    d.add_argument("--date", help="дата в имени файла очереди (по умолчанию сегодня)")
    d.add_argument("--dry-run", action="store_true", help="показать, куда ляжет, и ничего не писать")
    d.set_defaults(func=cmd_deliver)

    c = sub.add_parser("check", parents=[common],
                       help="сверить upstream-pending с очередью канона")
    c.add_argument("--redeliver", action="store_true",
                   help="доставить в очередь то, чего в ней нет")
    c.add_argument("--restore-local", action="store_true", dest="restore_local",
                   help="восстановить проектную копию по файлу очереди "
                        "(имя брифа выводится из имени файла - см. вывод check)")
    c.set_defaults(func=cmd_check)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
