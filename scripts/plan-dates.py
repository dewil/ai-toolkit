#!/usr/bin/env python3
"""Пересчет держателей, очистка готового и проверка Markdown-плана (Python 3.11+).

Без --write изменения рассчитываются в памяти. Остальной текст плана,
включая переводы строк, сохраняется при записи без изменений.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import stat
from statistics import median
import sys
import tempfile
from zoneinfo import ZoneInfo


MSK = ZoneInfo('Europe/Moscow')
DAY = timedelta(days=1)
STAMP = r'([0-9]{2})\.([0-9]{2}) ([0-9]{2}):([0-9]{2})'
HOLDER_COLUMNS = ['Держатель', 'Обычно, дн', 'Затяжно, дн',
                  'Замеров, шт', 'Открыто, шт']


class PlanError(Exception):
    """Ошибка вызова или данных с безопасным сообщением для пользователя."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # Сообщение argparse может включать содержимое неверного аргумента.
        raise PlanError('Неверные аргументы; справка: --help.')


def iso_time(value):
    if not isinstance(value, str):
        raise PlanError('В данных ожидается время в формате ISO8601.')
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=MSK)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise PlanError('Некорректное время в формате ISO8601.') from None


def load_data(path):
    try:
        data = json.loads(path.read_bytes().decode('utf-8'))
    except (ValueError, RecursionError):
        raise PlanError('Не удалось прочитать данные: ожидается корректный JSON UTF-8.') from None
    if not isinstance(data, dict):
        raise PlanError('Данные JSON должны быть объектом.')
    holders = data.get('holders', {})
    waits = data.get('open_waits', [])
    if not isinstance(holders, dict) or not isinstance(waits, list):
        raise PlanError('Некорректная форма holders или open_waits.')
    for wait in waits:
        if (not isinstance(wait, dict) or not isinstance(wait.get('holder'), str)
                or not isinstance(wait.get('id'), str)):
            raise PlanError('Ожидание должно содержать строковые holder и id.')
        iso_time(wait.get('since'))
    return holders, waits


def holder_stats(holders):
    result = {}
    for key, config in holders.items():
        if not isinstance(config, dict):
            raise PlanError('Описание держателя должно быть объектом.')
        label = config.get('label', key)
        defaults = config.get('default', [1, 3])
        samples = config.get('samples', [])
        if not isinstance(label, str) or not isinstance(samples, list):
            raise PlanError('Некорректные label или samples держателя.')
        if (not isinstance(defaults, list) or len(defaults) != 2
                or any(isinstance(n, bool) or not isinstance(n, (int, float))
                       or not math.isfinite(n) for n in defaults)):
            raise PlanError('Норма default должна содержать два конечных числа дней.')
        closed = []
        for sample in samples:
            if not isinstance(sample, dict):
                raise PlanError('Замер должен быть объектом.')
            if not sample.get('asked') or not sample.get('answered'):
                continue
            asked, answered = iso_time(sample['asked']), iso_time(sample['answered'])
            if answered >= asked:
                closed.append((answered, (answered - asked) / DAY))
        count = len(closed)
        if count >= 5:
            usual = median(duration for _, duration in closed)
            recent = sorted(closed, key=lambda item: item[0])[-10:]
            long = sorted(duration for _, duration in recent)[-2]
            measurements = str(count)
        else:
            usual, long = defaults
            measurements = f'{count} (по умолчанию: мало данных)'
        result[key] = (label, max(1, math.ceil(usual)), max(1, math.ceil(long)), measurements)
    return result


def visible_lines(lines):
    """Кодовые блоки Markdown не являются разделами или таблицами плана."""
    fence = None
    for index, line in enumerate(lines):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is None:
            yield index, line.rstrip('\r\n')


def sections(lines):
    starts = [(i, line[3:].strip()) for i, line in visible_lines(lines)
              if line.startswith('## ')]
    return [(name, start + 1, starts[n + 1][0] if n + 1 < len(starts) else len(lines))
            for n, (start, name) in enumerate(starts)]


def cells(line):
    value = line.strip()
    if not value.startswith('|'):
        return None
    value = value[1:]
    if value.endswith('|') and not value.endswith('\\|'):
        value = value[:-1]
    return [part.strip().replace('\\|', '|') for part in re.split(r'(?<!\\)\|', value)]


def tables(lines):
    visible = dict(visible_lines(lines))
    index = 0
    while index + 1 < len(lines):
        head = cells(visible.get(index, ''))
        separator = cells(visible.get(index + 1, ''))
        if (head is None or not separator
                or not all(re.fullmatch(r':?-{3,}:?', part) for part in separator)):
            index += 1
            continue
        end = index + 2
        while end < len(lines) and cells(visible.get(end, '')) is not None:
            end += 1
        yield index, end, head
        index = end


def newline(line):
    if line.endswith('\r\n'):
        return '\r\n'
    return '\n' if line.endswith('\n') else ''


def table_cell(value):
    return str(value).replace('\r', ' ').replace('\n', ' ').replace('|', '\\|')


def recalc(lines, stats, waits):
    section = next(((start, end) for name, start, end in sections(lines)
                    if name == 'Держатели'), None)
    if section is None:
        raise PlanError('Нет обязательного раздела "## Держатели".')
    start, end = section
    table = next(((a, b) for a, b, head in tables(lines)
                  if start <= a < end and head == HOLDER_COLUMNS), None)
    if table is None:
        raise PlanError('В разделе "Держатели" нет таблицы с нужными колонками.')
    first, last = table
    eol = newline(lines[first]) or '\n'
    counts = Counter(wait['holder'] for wait in waits)
    rows = ['| ' + ' | '.join(table_cell(value) for value in (*values, counts[key])) + ' |'
            for key, values in stats.items()]
    # Шапку и разделитель таблицы сохраняем; заменяем только строки данных.
    tail_eol = newline(lines[last - 1])
    replacement = [row + eol for row in rows]
    if replacement and not tail_eol:
        replacement[-1] = replacement[-1].removesuffix(eol)
    return lines[:first + 2] + replacement + lines[last:]


def plan_time(match, now):
    if match is None:
        return None
    day, month, hour, minute = map(int, match.groups())
    year = now.astimezone(MSK).year
    try:
        parsed = datetime(year, month, day, hour, minute, tzinfo=MSK)
        if parsed - now > DAY:
            parsed = parsed.replace(year=year - 1)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def done_time(line, now):
    leading = re.match(r'^- ' + STAMP + r'(?![0-9])', line)
    if leading:
        return plan_time(leading, now)
    trailing = re.search(r'\([^()]*\)\s*$', line)
    matches = list(re.finditer(STAMP + r'(?![0-9])', trailing.group())) if trailing else []
    return plan_time(matches[-1] if matches else None, now)


def done_items(lines, now):
    visible = dict(visible_lines(lines))
    for name, start, end in sections(lines):
        if name != 'Готово за сутки':
            continue
        for index in range(start, end):
            if visible.get(index, '').startswith('- '):
                yield index, done_time(visible[index], now)


def prune(lines, now):
    removed = set()
    for index, stamp in done_items(lines, now):
        if stamp is None or now - stamp <= DAY:
            continue
        removed.add(index)
        index += 1
        while index < len(lines) and lines[index].startswith((' ', '\t')):
            removed.add(index)
            index += 1
    return [line for index, line in enumerate(lines) if index not in removed], len(removed)


def safe_id(value):
    return ''.join(ch if ch.isprintable() else '?' for ch in value)


def lint(lines, stats, waits, now):
    findings = []

    def report(level, message):
        findings.append(f'{level}: {message}')

    visible = list(visible_lines(lines))
    header_end = next((i for i, line in visible if line.startswith('## ')), len(lines))
    header = [(i, line) for i, line in visible if i < header_end]
    updates = [(i, line) for i, line in header if line.startswith('Обновлено:')]
    stamp = None
    if len(updates) == 1:
        stamp = plan_time(re.match(r'^Обновлено: ' + STAMP + r' MSK\.', updates[0][1]), now)
    if stamp is None:
        report('ERROR', 'В шапке нужна ровно одна строка "Обновлено: ДД.ММ ЧЧ:ММ MSK.".')
    elif now - stamp > DAY:
        report('WARN', 'Отметка "Обновлено" старше 24 часов.')
    title = next((i for i, line in header if line.startswith('# ')), -1)
    if sum(bool(line.strip()) for i, line in header if i > title) > 5:
        report('WARN', 'В шапке больше 5 непустых строк.')

    all_tables = list(tables(lines))
    for start, end, head in all_tables:
        seen = set()
        for index in range(start + 1, end):
            row = cells(lines[index])
            if len(row) != len(head):
                report('ERROR', f'Строка {index + 1}: число колонок отличается от заголовка.')
            if index == start + 1:
                continue
            key = tuple(row)
            if key in seen:
                report('ERROR', f'Строка {index + 1}: дубль строки внутри таблицы.')
            seen.add(key)

    for name, section_start, section_end in sections(lines):
        counted = re.fullmatch(r'(Блокировки - кого ждем|В работе сейчас|Риски) - ([0-9]+)', name)
        if counted is None:
            continue
        kind, expected = counted.groups()
        count = 0
        for start, end, head in all_tables:
            if not section_start <= start < section_end:
                continue
            for index in range(start + 2, end):
                row = dict(zip(head, cells(lines[index])))
                if kind == 'Блокировки - кого ждем' and 'ничего не ждем' in row.get('Что', '').lower():
                    continue
                count += 1
                required = {'Блокировки - кого ждем': ('Снимется', 'Где'),
                            'В работе сейчас': ('Готово',), 'Риски': ()}[kind]
                for column in required:
                    if not row.get(column, '').strip():
                        report('ERROR', f'Строка {index + 1}: пустая колонка "{column}".')
        if count != int(expected):
            report('ERROR', f'Раздел "{kind}": число N не равно числу строк таблицы ({count}).')
        if kind == 'Риски' and count > 7:
            report('WARN', 'В таблице рисков больше 7 строк.')

    for index, stamp in done_items(lines, now):
        if stamp is None:
            report('ERROR', f'Строка {index + 1}: элемент "Готово за сутки" без читаемого времени.')
        elif now - stamp > DAY:
            report('WARN', f'Строка {index + 1}: элемент "Готово за сутки" старше 24 часов.')
    for wait in waits:
        holder = wait['holder']
        identity = safe_id(wait['id'])
        if holder not in stats:
            report('WARN', f'Ожидание {identity}: неизвестный держатель.')
        elif (now - iso_time(wait['since'])) / DAY > stats[holder][2]:
            report('WARN', f'Ожидание {identity} горит: превышена норма "затяжно".')
    return findings


def write_atomic(path, content):
    mode = stat.S_IMODE(path.stat().st_mode)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.plan-dates-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink()


def main(argv=None):
    parser = Parser(description=__doc__)
    parser._positionals.title = 'Команда'
    parser._optionals.title = 'Параметры'
    parser.add_argument('command', choices=('recalc', 'prune', 'lint', 'all'), help='Операция с планом')
    parser.add_argument('--plan', type=Path, required=True, help='Файл плана Markdown')
    parser.add_argument('--data', type=Path, required=True, help='Файл данных JSON')
    parser.add_argument('--now', help='Текущее время ISO8601 с часовым поясом')
    parser.add_argument('--write', action='store_true', help='Записать изменения атомарно')
    try:
        args = parser.parse_args(argv)
        if args.now is not None:
            now = iso_time(args.now)
            if datetime.fromisoformat(args.now).utcoffset() is None:
                raise PlanError('--now требует часовой пояс.')
        else:
            now = datetime.now(timezone.utc)
        original = args.plan.read_bytes()
        lines = original.decode('utf-8').splitlines(keepends=True)
        holders, waits = load_data(args.data)
        stats = holder_stats(holders)
        messages = []
        if args.command in ('recalc', 'all'):
            changed = recalc(lines, stats, waits)
            if changed != lines:
                messages.append('Пересчет таблицы "Держатели".')
            lines = changed
        if args.command in ('prune', 'all'):
            lines, count = prune(lines, now)
            if count:
                messages.append(f'Очистка "Готово за сутки": удаляемых строк {count}.')
        findings = lint(lines, stats, waits, now) if args.command in ('lint', 'all') else []
        content = ''.join(lines).encode('utf-8')
        if args.write and content != original:
            write_atomic(args.plan, content)
        for message in messages:
            print(('Записано: ' if args.write else 'Будет изменено: ') + message)
        for finding in findings:
            print(finding)
        return int(any(finding.startswith('ERROR:') for finding in findings))
    except PlanError as error:
        print(f'ERROR: {error}')
    except UnicodeError:
        print('ERROR: План должен быть в кодировке UTF-8.')
    except OSError:
        print('ERROR: Не удалось прочитать файлы или атомарно записать план; проверьте пути и права.')
    return 2


if __name__ == '__main__':
    sys.exit(main())
