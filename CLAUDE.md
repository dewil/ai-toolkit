# claude-toolkit

Канон правил, агентов и bootstrap-промтов для проектов с Claude Code. Этот файл - **контекст для Claude, работающего внутри этого репозитория** (правка канона, добавление новых rules/agents, рефакторинг bootstrap-промтов).

## Что это за репо

`claude-toolkit` - источник правды для:

- Правил поведения Claude в проектах (`rules/`).
- Описаний субагентов (`agents/`).
- Канонических скиллов (`skills/`).
- Канонических исполняемых скриптов проекта (`scripts/`).
- Bootstrap-цепочки для настройки новых проектов (`bootstrap/`).
- Одноразовых миграционных промтов для апгрейда существующих проектов (`migrations/`).

Проекты подключают канон по **HTTP** через `raw.githubusercontent.com` - не клонят репо, не симлинкают папки. Снимки канонических файлов копируются в `.claude/rules/`, `.claude/agents/`, `.claude/skills/` проекта (и в `scripts/` в корне проекта - для канон-категории `scripts/` маппинг без `.claude/`-префикса), проект помнит источник в `.claude/canon.yaml`. Sync сравнивает снимки с каноном и предлагает обновления.

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
- `claude-tooling` - проекты-конструкторы для Claude, как этот репо (скелет).
- `wiki` - Obsidian-vault'ы, персональные базы знаний (заметки, перелинковка `[[...]]`).

Проект может быть **мультиспециализированным** - например, vault Obsidian со встроенной кодовой частью и документацией будет иметь `project_type: [wiki, coding, documentation]`. В bootstrap-цепочке на шаге `bootstrap-02` пользователь выбирает несколько типов; bootstrap-03 запускается **последовательно по каждому** из них, накладывая специализации друг на друга. Каждый bootstrap-03 идемпотентен и только **добавляет** свой тип в список (если еще не там), не перезаписывая.

Каждый шаг - сиблинг в той же папке `bootstrap/`. Сосед адресуется как `<dirname(этого файла)>/<имя соседа>`. Пользователя не спрашиваем - выводим путь сами.

### canon.yaml в проекте

После bootstrap в проекте появляется `.claude/canon.yaml`:

```yaml
project_type: [coding]  # список типов из набора: coding | management | education | documentation | claude-tooling | wiki. Может содержать несколько (напр. [wiki, coding, documentation] для vault'а со встроенной кодовой частью и доками).
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
```

`canon.yaml` коммитится в репозиторий проекта. Источник истины для sync'а. Поле `project_type` - **всегда список**, наполняется на шаге `bootstrap-03-<тип>` (по одному типу за прогон). Используется sync'ом для определения, какие новые канон-файлы предлагать (autodiscovery идет по всем типам списка + `universal`).

### Sync

В существующем проекте пользователь говорит "сделай синк с canon" - Claude читает `.claude/canon.yaml`, фетчит каждый `files[i]` с `raw_base`, сравнивает с локальным, спрашивает по каждому "обновить? (да/нет/diff)". Логика - в `migrations/sync-from-canon.prompt.md`.

Помимо отслеживаемых файлов sync делает autodiscovery: фетчит `manifest.yaml` из корня репо (полный список канон-файлов по типам) и предлагает добавить новые канон-файлы своего типа, появившиеся после bootstrap проекта.

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
│   ├── bootstrap-01-memory.prompt.md            # симлинк памяти + feedback_local_paths
│   ├── bootstrap-02-scaffold.prompt.md          # base scaffold + canon.yaml
│   ├── bootstrap-03-coding.prompt.md
│   ├── bootstrap-03-management.prompt.md
│   ├── bootstrap-03-education.prompt.md
│   ├── bootstrap-03-documentation.prompt.md
│   ├── bootstrap-03-claude-tooling.prompt.md
│   └── bootstrap-03-wiki.prompt.md
├── rules/                                       # КАНОН: правила поведения Claude
│   ├── typography-ru.md                         # universal
│   ├── subagents-usage.md                       # universal
│   ├── docs-maintenance.md                      # universal
│   ├── karpathy-guidelines.md                   # coding-специализация
│   ├── tests-coverage.md                        # coding-специализация
│   ├── error-exposure.md                        # coding-специализация
│   ├── prompt-conventions.md                    # claude-tooling-специализация
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
│   ├── prompt-reviewer.md                       # claude-tooling-специализация
│   ├── note-writer.md                           # wiki-специализация
│   ├── librarian.md                             # wiki-специализация
│   ├── tracker.md                               # management-специализация
│   ├── note-taker.md                            # education-специализация
│   └── tutor.md                                 # education-специализация
├── skills/                                      # КАНОН: скиллы (папка на скилл)
│   ├── codex-audit/                             # universal
│   │   └── SKILL.md
│   └── telegram-snapshot-setup/                 # management-специализация
│       └── SKILL.md
├── scripts/                                     # КАНОН: проектные скрипты (путь в каноне = путь в проекте)
│   ├── telegram-snapshot.py                     # management-специализация
│   └── telegram-deltas.py                       # management-специализация
├── templates/                                   # ШАБЛОНЫ: копируются в проект один раз
│   ├── project-structure.md                     # universal  -> .claude/rules/project-structure.md
│   ├── style-guide.md                           # documentation -> .claude/rules/style-guide.md
│   ├── wiki-structure.md                        # wiki -> .claude/rules/wiki-structure.md
│   └── management-CLAUDE.md                     # management -> CLAUDE.md в корне
└── migrations/                                  # одноразовые промты
    └── sync-from-canon.prompt.md                # синхронизация проекта с каноном
```

Семантика `templates/` отличается от `rules/`, `agents/`, `skills/`, `scripts/`:

- **Канон-файлы** (`rules/`, `agents/`, `skills/`, `scripts/`) - источник истины, проект отслеживает их в `canon.yaml.files`, sync поддерживает в актуальном состоянии. У `scripts/` маппинг тривиальный: путь в каноне = путь в проекте (`scripts/X.py` -> `scripts/X.py`).
- **Шаблоны** (`templates/`) - скелеты с TODO-заглушками. Bootstrap копирует их один раз; дальше проект владеет файлом сам, заполняет под себя. В `canon.yaml.files` НЕ заносятся, sync их не контролирует, `manifest.yaml` про них не знает. Это намеренно: после копирования файл разойдется с шаблоном, и пытаться его "синкать" бессмысленно.

## Принципы (применяются в каждом промте этого репо)

- **Идемпотентность.** Любой промт безопасен при повторном запуске. На свежем/актуальном проекте отрабатывает как no-op с явным сообщением.
- **Audit -> plan -> "ок" -> action.** Любая правка делается только после явного подтверждения. Без "ок" - только чтение. Каждый bootstrap/migration промт начинается с ШАГ 1 "Аудит", за ним ШАГ 2 "План", дальше ждет "ок".
- **Строгая изоляция.** Проект, в который раскатывается канон, ничего не знает о filesystem-расположении локального клона `claude-toolkit` на машине пользователя. Все взаимодействие - через HTTP (`raw.githubusercontent.com`) либо через самодостаточные артефакты (upstream-candidate брифы).
- **Канон в одном экземпляре.** Один rule = одна каноническая версия в `rules/<name>.md`. Никаких heredoc-копий канонического контента в bootstrap-промтах. Если нужно положить правило в проект - WebFetch/cp из канона. То же касается шаблонов: каждый template = один файл в `templates/<name>.md`, никаких heredoc'ов в промтах.
- **Sibling-paths без user-asking.** Каждый промт выводит путь к следующему/соседу из своего собственного источника (URL или filesystem path), не спрашивает у пользователя. Спрашивает только если по выведенному адресу файла физически нет.
- **Русская типографика.** Кавычки `"..."`, тире и дефис - один и тот же символ `-` (U+002D), "е" вместо "ё". См. `rules/typography-ru.md`.

## Регистрация нового канон-файла

При добавлении нового правила/агента/скилла/скрипта в `rules/`, `agents/`, `skills/` или `scripts/`:

- Зарегистрировать в `manifest.yaml` в нужной секции (`universal` / `coding` / `documentation` / `claude-tooling` / `wiki` / `management` / `education`).
- Зарегистрировать в соответствующем bootstrap-промте:
  - Универсальный файл -> `bootstrap-02-scaffold.prompt.md` (ШАГ 2 списки + шаблон `canon.yaml -> files`).
  - Файл под тип проекта -> соответствующий `bootstrap-03-<тип>.prompt.md` (ШАГ 2 списки + дописывание в `canon.yaml -> files` на ШАГ 4). bootstrap-03-* тянет свой набор сам через WebFetch из `<canon_base>`, без heredoc.
- Для скриптов в `scripts/<name>.py` - целевой путь в проекте совпадает с каноническим (`scripts/<name>.py`); bootstrap-промт также делает `chmod +x` на скопированный файл.
- Обновить дерево репо в разделе "Структура репо" выше - дописать файл с пометкой типа.

## Регистрация нового шаблона

При добавлении нового шаблона в `templates/`:

- Положить файл в `templates/<name>.md`. Содержимое - скелет с TODO-заглушками.
- В соответствующем bootstrap-промте (`bootstrap-02-scaffold.prompt.md` для универсального или `bootstrap-03-<тип>.prompt.md` для типового) добавить шаг "создать `<куда>` из шаблона" по той же схеме, что для существующих:
  - HTTP: `WebFetch <canon_base>/templates/<name>.md` -> запиши в `<путь в проекте>`.
  - Локально: `cp <canon_base>/templates/<name>.md <путь в проекте>`.
  - Существующий файл НЕ перезаписывается (идемпотентность).
- В `manifest.yaml` шаблоны НЕ заносятся (sync их не использует).
- В `canon.yaml.files` проекта шаблоны НЕ записываются (после копирования проект владеет файлом сам).
- Обновить дерево репо в разделе "Структура репо" выше - дописать шаблон в `templates/` с пометкой "<тип> -> <куда копируется в проекте>".

## Правила работы с этим репо

@./rules/karpathy-guidelines.md
@./rules/subagents-usage.md
@./rules/docs-maintenance.md
@./rules/typography-ru.md
