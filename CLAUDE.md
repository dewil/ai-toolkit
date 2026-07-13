# claude-toolkit

Канон правил, агентов и bootstrap-промтов для проектов с Claude Code. Этот файл - **контекст для Claude, работающего внутри этого репозитория** (правка канона, добавление новых rules/agents, рефакторинг bootstrap-промтов).

## Что это за репо

`claude-toolkit` - источник правды для:

- Правил поведения Claude в проектах (`rules/`).
- Описаний субагентов (`agents/`).
- Канонических скиллов (`skills/`).
- Канонических слэш-команд (`commands/`).
- Канонических исполняемых скриптов проекта (`scripts/`).
- Bootstrap-цепочки для настройки новых проектов (`bootstrap/`).
- Одноразовых миграционных промтов для апгрейда существующих проектов (`migrations/`).

Проекты подключают канон по **HTTP** через `raw.githubusercontent.com` - не клонят репо, не симлинкают папки. Снимки канонических файлов копируются в `.claude/rules/`, `.claude/agents/`, `.claude/skills/`, `.claude/commands/` проекта (и в `scripts/` в корне проекта - для канон-категории `scripts/` маппинг без `.claude/`-префикса), проект помнит источник в `.claude/canon.yaml`. Sync сравнивает снимки с каноном и предлагает обновления.

## Архитектура

### Точка входа

`start.md` в корне репо. Пользователь в любом проекте говорит:

```
выполни инструкции из https://raw.githubusercontent.com/dewil/claude-toolkit/main/start.md
```

`start.md` - тонкий роутер, спрашивает у пользователя цель (новый проект / sync / миграция) и направляет в соответствующий промт.

### Bootstrap-цепочка для нового проекта

`start.md` -> `bootstrap/bootstrap-01-memory.prompt.md` -> `bootstrap/bootstrap-02-scaffold.prompt.md` -> `bootstrap/bootstrap-03-<тип>.prompt.md` (по одному прогону на каждый выбранный тип)

Типы проекта:

- `coding` - кодовые проекты.
- `management` - управленческие.
- `education` - учебные.
- `documentation` - документация (скелет, требует наполнения).
- `wiki` - Obsidian-vault'ы, персональные базы знаний (заметки, перелинковка `[[...]]`).

Проект может быть **мультиспециализированным** - например, vault Obsidian со встроенной кодовой частью и документацией будет иметь `project_type: [wiki, coding, documentation]`. В bootstrap-цепочке на шаге `bootstrap-02` пользователь выбирает несколько типов; bootstrap-03 запускается **последовательно по каждому** из них, накладывая специализации друг на друга. Каждый bootstrap-03 идемпотентен и только **добавляет** свой тип в список (если еще не там), не перезаписывая.

Каждый шаг - сиблинг в той же папке `bootstrap/`. Сосед адресуется как `<dirname(этого файла)>/<имя соседа>`. Пользователя не спрашиваем - выводим путь сами.

### canon.yaml в проекте

После bootstrap в проекте появляется `.claude/canon.yaml`:

```yaml
project_type: [coding]  # список типов из набора: coding | management | education | documentation | wiki. Может содержать несколько (напр. [wiki, coding, documentation] для vault'а со встроенной кодовой частью и доками).
canon:
  repo: https://github.com/dewil/claude-toolkit
  raw_base: https://raw.githubusercontent.com/dewil/claude-toolkit/main
  branch: main
  bootstrapped_at: 2026-05-22
files:
  - rules/typography-ru.md
  - rules/karpathy-guidelines.md
  - ...
local_only: []        # файлы в проекте, которых нет в каноне
skip_sync: []         # есть в каноне, проект сознательно не накатывает
upstream_pending: []  # помечены к выносу в канон
file_hashes:          # sha256 канонических байт каждого files[i] на момент установки/синка - база снимка
  rules/typography-ru.md: <sha256>
  ...
```

`canon.yaml` коммитится в репозиторий проекта. Источник истины для sync'а. Поле `project_type` - **всегда список**, наполняется на шаге `bootstrap-03-<тип>` (по одному типу за прогон). Используется sync'ом для определения, какие новые канон-файлы предлагать (autodiscovery идет по всем типам списка + `universal`).

`file_hashes` - **база снимка**: хеш канонических байт каждого файла на момент его последней установки (bootstrap) или обновления (sync). Дает sync'у трехстороннее сравнение (база / локальная копия / текущий канон) и позволяет различать "проект правил файл локально" от "канон ушел вперед" вместо бинарного "разошелся". Заполняется по мере раскатки/синка; отсутствие записи = база неизвестна, sync деградирует к бинарному сравнению и проставляет хеш при первом совпадении/обновлении. Хеши - только для путей из `files[]`.

### Sync

В существующем проекте пользователь говорит "сделай синк с canon" (или "sync canon") - Claude читает `.claude/canon.yaml`, фетчит каждый `files[i]` с `raw_base` (точными байтами через `curl`), сравнивает **трехсторонне** - база (`file_hashes`) / локальная копия / текущий канон - и по типу расхождения (устарел / локальная правка / конфликт / база неизвестна) предлагает безопасное действие. Логика - в `migrations/sync-from-canon.prompt.md`.

Помимо отслеживаемых файлов sync делает autodiscovery: фетчит `manifest.yaml` из корня репо (полный список канон-файлов по типам) и предлагает добавить новые канон-файлы своего типа, появившиеся после bootstrap проекта. Autodiscovery - обязательная половина аудита (а не диф `files[]`), без нее новые канон-файлы в проект не попадают вовсе; промт ставит на нее жесткий гейт. Заодно sync ловит **неполный `project_type`**: если в проекте лежат файлы типа, которого нет в списке (улика), предлагает дополнить `project_type`, иначе autodiscovery этой секции молча отключен.

### Upstream candidates

Если в проекте появилось ценное правило, которое стоит вынести в канон, sync:

1. Создает `toolkit-log/upstream-pending/<slug>.md` в проекте - самодостаточный бриф для Claude в локальном клоне claude-toolkit. Содержит путь в каноне, содержимое, rationale.
2. В `canon.yaml -> upstream_pending` добавляет запись.
3. **Проект ничего не знает о filesystem-расположении клона claude-toolkit на машине пользователя.** Пользователь руками открывает свой клон, скармливает бриф местному Claude, ревьюит, коммитит, пушит.
4. На следующем sync проект видит, что файл доехал в канон, чистит запись из upstream_pending и переносит бриф в `toolkit-log/upstream-applied/`.

## Структура репо

```
claude-toolkit/
├── README.md                                    # для GitHub-посетителей
├── CLAUDE.md                                    # этот файл
├── start.md                                     # точка входа (тонкий роутер)
├── manifest.yaml                                # полный список канон-файлов по типам (для sync autodiscovery)
├── bootstrap/
│   ├── bootstrap-01-memory.prompt.md            # симлинк памяти + feedback-сиды (локальные пути, изменения канона)
│   ├── bootstrap-02-scaffold.prompt.md          # base scaffold + canon.yaml
│   ├── bootstrap-03-coding.prompt.md
│   ├── bootstrap-03-management.prompt.md
│   ├── bootstrap-03-education.prompt.md
│   ├── bootstrap-03-documentation.prompt.md
│   └── bootstrap-03-wiki.prompt.md
├── rules/                                       # КАНОН: правила поведения Claude
│   ├── typography-ru.md                         # universal
│   ├── subagents-usage.md                       # universal
│   ├── docs-maintenance.md                      # universal
│   ├── compact-results.md                       # universal
│   ├── addressing.md                            # universal
│   ├── secrets-handling.md                      # universal
│   ├── karpathy-guidelines.md                   # coding-специализация
│   ├── tests-coverage.md                        # coding-специализация
│   ├── error-exposure.md                        # coding-специализация
│   ├── wiki-notes-style.md                      # wiki-специализация
│   ├── wiki-linking-obsidian.md                 # wiki-специализация
│   ├── meetings.md                              # management-специализация
│   ├── tasks-tracking.md                        # management-специализация
│   ├── artifacts-structure.md                   # management-специализация
│   ├── meeting-transcripts.md                   # management-специализация
│   ├── name-cross-check.md                      # management-специализация
│   ├── google-sheets-mcp.md                     # management-специализация
│   ├── estimates-in-hours.md                    # management-специализация
│   ├── lecture-notes.md                         # education-специализация
│   ├── homework.md                              # education-специализация
│   └── course-structure.md                      # education-специализация
├── agents/                                      # КАНОН: описания субагентов
│   ├── architect.md                             # universal
│   ├── explorer.md                              # universal
│   ├── searcher.md                              # universal
│   ├── planner.md                               # universal
│   ├── summarizer.md                            # universal
│   ├── debugger.md                              # coding-специализация
│   ├── implementer.md                           # coding-специализация
│   ├── code-reviewer.md                         # coding-специализация
│   ├── copy-editor.md                           # documentation-специализация
│   ├── note-writer.md                           # wiki-специализация
│   ├── librarian.md                             # wiki-специализация
│   ├── tracker.md                               # management-специализация
│   └── note-taker.md                            # education-специализация
├── skills/                                      # КАНОН: скиллы (папка на скилл)
│   ├── codex-audit/                             # universal
│   │   └── SKILL.md
│   ├── web-research/                            # universal
│   │   └── SKILL.md
│   ├── md-pdf/                                  # universal
│   │   └── SKILL.md
│   ├── minimal-code/                            # coding-специализация
│   │   └── SKILL.md
│   ├── telegram-snapshot/                       # management + wiki
│   │   └── SKILL.md
│   ├── telegram-send/                           # management + wiki
│   │   └── SKILL.md
│   ├── redmine-snapshot/                        # management
│   │   └── SKILL.md
│   ├── mymeet-snapshot/                         # management
│   │   └── SKILL.md
│   └── tutor/                                   # education-специализация
│       └── SKILL.md
├── commands/                                    # КАНОН: слэш-команды (файл на команду)
│   └── canon.md                                 # universal -> .claude/commands/canon.md (/canon = sync)
├── scripts/                                     # КАНОН: проектные скрипты (путь в каноне = путь в проекте)
│   ├── md-pdf.py                                # universal
│   ├── telegram-snapshot.py                     # management + wiki
│   ├── telegram-deltas.py                       # management + wiki
│   ├── telegram-send.py                         # management + wiki
│   ├── redmine-snapshot.py                      # management
│   ├── redmine-deltas.py                        # management
│   └── mymeet-snapshot.py                       # management
├── templates/                                   # ШАБЛОНЫ: копируются в проект один раз
│   ├── project-structure.md                     # universal  -> project-structure.md (в корне проекта)
│   ├── style-guide.md                           # documentation -> .claude/rules/style-guide.md
│   ├── wiki-conventions.md                      # wiki -> .claude/rules/wiki-conventions.md
│   └── management-CLAUDE.md                     # management -> CLAUDE.md в корне
└── migrations/                                  # одноразовые промты
    ├── move-project-structure.prompt.md         # перенос project-structure.md в корень (.claude/rules -> корень)
    └── sync-from-canon.prompt.md                # синхронизация проекта с каноном
```

Семантика `templates/` отличается от `rules/`, `agents/`, `skills/`, `commands/`, `scripts/`:

- **Канон-файлы** (`rules/`, `agents/`, `skills/`, `commands/`, `scripts/`) - источник истины, проект отслеживает их в `canon.yaml.files`, sync поддерживает в актуальном состоянии. У `scripts/` маппинг тривиальный: путь в каноне = путь в проекте (`scripts/X.py` -> `scripts/X.py`).
- **Шаблоны** (`templates/`) - скелеты с TODO-заглушками. Bootstrap копирует их один раз; дальше проект владеет файлом сам, заполняет под себя. В `canon.yaml.files` НЕ заносятся, sync их не контролирует, `manifest.yaml` про них не знает. Это намеренно: после копирования файл разойдется с шаблоном, и пытаться его "синкать" бессмысленно.

## Принципы (применяются в каждом промте этого репо)

- **Идемпотентность.** Любой промт безопасен при повторном запуске. На свежем/актуальном проекте отрабатывает как no-op с явным сообщением.
- **Audit -> plan -> "ок" -> action.** Любая правка делается только после явного подтверждения. Без "ок" - только чтение. Каждый bootstrap/migration промт начинается с ШАГ 1 "Аудит", за ним ШАГ 2 "План", дальше ждет "ок".
- **Строгая изоляция.** Проект, в который раскатывается канон, ничего не знает о filesystem-расположении локального клона `claude-toolkit` на машине пользователя и **не ищет его**. Канон тянется **только по HTTP** (`raw.githubusercontent.com`) с `<canon_base>`, даже если клон физически лежит рядом с проектом - локальный клон не подхватывается и не используется как источник; наверх - только через самодостаточные артефакты (upstream-candidate брифы). bootstrap-цепочка (`start.md`, `bootstrap-*`) и sync держат это правило одинаково строго.
- **Канон в одном экземпляре.** Один rule = одна каноническая версия в `rules/<name>.md`. Никаких heredoc-копий канонического контента в bootstrap-промтах. Если нужно положить правило в проект - `curl`/`cp` из канона (точными байтами). То же касается шаблонов: каждый template = один файл в `templates/<name>.md`, никаких heredoc'ов в промтах. **Списки канон-файлов тоже в одном экземпляре** - в `manifest.yaml`: bootstrap-промты не перечисляют файлы руками, а тянут свою секцию из манифеста и раскатывают ее.
- **Sibling-paths без user-asking.** Каждый промт выводит путь к следующему/соседу из своего собственного источника (URL или filesystem path), не спрашивает у пользователя. Спрашивает только если по выведенному адресу файла физически нет.
- **Русская типографика.** Кавычки `"..."`, тире и дефис - один и тот же символ `-` (U+002D), "е" вместо "ё". См. `rules/typography-ru.md`.

## Регистрация нового канон-файла

При добавлении нового правила/агента/скилла/команды/скрипта в `rules/`, `agents/`, `skills/`, `commands/` или `scripts/`:

- **Зарегистрировать в `manifest.yaml`** в нужной секции (`universal` / `coding` / `documentation` / `wiki` / `management` / `education`). Это **единственный реестр списков**: bootstrap-02 (секция `universal`) и bootstrap-03-* (своя секция) тянут список из манифеста и раскатывают его - перечислять файл в bootstrap-промтах руками больше не нужно. Sync видит новый файл через autodiscovery по тому же манифесту.
- Если файл - **правило** (`rules/<name>.md`), bootstrap добавит на него `@`-импорт в `CLAUDE.md` проекта автоматически (по одному импорту на каждое правило секции) - дописывать в промт ничего не надо.
- Для скриптов в `scripts/<name>.py` - целевой путь в проекте совпадает с каноническим (`scripts/<name>.py`); bootstrap делает `chmod +x` на скопированный файл (generic-маппинг по префиксу `scripts/`).
- Для команд в `commands/<name>.md` - целевой путь `.claude/commands/<name>.md` (как rules/agents/skills, без `chmod`); в проекте вызывается как `/<name>`. В `CLAUDE.md` проекта команды не импортируются (Claude Code находит их в `.claude/commands/` сам).
- Обновить дерево репо в разделе "Структура репо" выше - дописать файл с пометкой типа.

## Регистрация нового шаблона

При добавлении нового шаблона в `templates/`:

- Положить файл в `templates/<name>.md`. Содержимое - скелет с TODO-заглушками.
- В соответствующем bootstrap-промте (`bootstrap-02-scaffold.prompt.md` для универсального или `bootstrap-03-<тип>.prompt.md` для типового) добавить шаг "создать `<куда>` из шаблона" по той же схеме, что для существующих:
  - `curl -fsSL <canon_base>/templates/<name>.md` -> запиши в `<путь в проекте>` (канон тянется только по HTTP; локальный клон не подхватываем).
  - Существующий файл НЕ перезаписывается (идемпотентность).
- В `manifest.yaml` шаблоны НЕ заносятся (sync их не использует).
- В `canon.yaml.files` проекта шаблоны НЕ записываются (после копирования проект владеет файлом сам).
- Обновить дерево репо в разделе "Структура репо" выше - дописать шаблон в `templates/` с пометкой "<тип> -> <куда копируется в проекте>".

## Правила работы с этим репо

@./rules/karpathy-guidelines.md
@./rules/subagents-usage.md
@./rules/docs-maintenance.md
@./rules/typography-ru.md
