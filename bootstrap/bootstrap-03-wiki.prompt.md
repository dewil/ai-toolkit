# Промт: scaffold для wiki-проектов (Obsidian)

Этот файл - инструкция для Claude Code. Запускать **после** `bootstrap-02-scaffold.prompt.md`: открой Claude Code в **корне vault'а** и попроси выполнить этот файл (например: `выполни инструкции из <путь>/bootstrap-03-wiki.prompt.md`).

Промт безопасен для существующих проектов. Сначала аудит, действия - после "ок". Существующие файлы не перезаписываются.

Подходит и для **миграции** уже бутстрапленного проекта другого типа (например, `management`) в `wiki`: промт только добавляет файлы wiki-специализации и не трогает существующее. Если проект уже имеет специализацию другого типа - см. ШАГ 1 (предупреждение).

Добавляет к общему scaffold:

- правила: `wiki-notes-style.md` (формат заметки), `wiki-linking-obsidian.md` (правила `[[ссылок]]`)
- агентов: `note-writer` (создает/правит заметки), `librarian` (read-only ревизия vault'а)
- локальный шаблон `rules/wiki-structure.md` (структура разделов, теги конкретного vault'а - заполняется проектом)
- `@-`ссылки на эти правила в `CLAUDE.md`

---

КОНТЕКСТ ЦЕПОЧКИ. Это шаг 03. **Источник этого файла** (URL или локальный путь) определяет базу для канонических файлов:

- `canon_base` = подняться на один уровень из `bootstrap/`. Например, если этот файл загружен по URL `.../main/bootstrap/bootstrap-03-wiki.prompt.md`, то `canon_base = .../main/`.
- Канонические правила и агенты специализации: `<canon_base>/rules/*.md`, `<canon_base>/agents/*.md`.
- Предыдущий шаг (сиблинг): `bootstrap-02-scaffold.prompt.md` в той же папке.

Ты в корне Obsidian-vault'а. Раскатываешь специализацию scaffold под wiki: правила формата заметки и перелинковки, агенты note-writer/librarian, шаблон структуры. Базовый scaffold должен быть уже на месте (bootstrap-02-scaffold).

ВАЖНО. Промт безопасен для существующих проектов. Сначала аудит, потом план, потом ждешь "ок", только потом действуешь. Существующие файлы НЕ перезаписываешь.

ШАГ 1. Аудит (только чтение).

- pwd - корень vault'а.
- Маркеры Obsidian-vault'а:
  - `.obsidian/` в корне - vault уже открывался в Obsidian.
  - Хотя бы одна заметка с `[[wiki-link]]`-синтаксисом (`grep -rE '\[\[[^]]+\]\]' --include='*.md' | head -5`).
  - Если ни одного маркера нет - сообщи и спроси: это точно Obsidian-проект или просто папка markdown'ов? Без подтверждения не действуй.
- Перечисли существующие:
  - .claude/rules/wiki-notes-style.md, .claude/rules/wiki-linking-obsidian.md, .claude/rules/wiki-structure.md
  - .claude/agents/note-writer.md, .claude/agents/librarian.md
  - .claude/canon.yaml - есть ли (должен быть после bootstrap-02), что в `files`
  - CLAUDE.md - какие @-ссылки уже есть
- Если в `.claude/canon.yaml -> files` уже есть файлы другого типа специализации (`agents/copy-editor.md` из documentation, или `agents/debugger.md`/`agents/implementer.md`/`agents/code-reviewer.md` из coding, или `rules/prompt-conventions.md`/`agents/prompt-reviewer.md` из claude-tooling) - это **миграция** между типами. Сообщи в плане: "Проект уже имеет специализацию <тип>. Wiki-специализация будет ДОБАВЛЕНА поверх. Существующие файлы не удаляются автоматически - если они не нужны, удали вручную после ШАГ 4 (см. раздел МИГРАЦИЯ в конце)".

ШАГ 2. Целевое состояние.

A. .claude/rules/ (источник - `<canon_base>/rules/`):
   - wiki-notes-style.md
   - wiki-linking-obsidian.md

B. .claude/agents/ (источник - `<canon_base>/agents/`):
   - note-writer.md
   - librarian.md

C. .claude/rules/:
   - wiki-structure.md - локально создаваемый шаблон с TODO (содержимое в ШАГ 4b). Не из канона: разделы и теги у каждого vault'а свои.

D. CLAUDE.md содержит ссылки `@.claude/rules/wiki-notes-style.md`, `@.claude/rules/wiki-linking-obsidian.md`, `@.claude/rules/wiki-structure.md`.

E. `.claude/canon.yaml`:
   - `files` содержит файлы wiki-специализации (A + B).
   - `project_type` равен `wiki` (если пусто/отсутствует - проставляется на ШАГ 4).

ШАГ 3. План.

Таблица: файл / действие / статус. Для существующих - "не трону". Для отсутствующих - "создам".

Для CLAUDE.md - если файла нет или нет нужных @-ссылок - покажи, что добавишь.

Для canon.yaml - покажи, какие записи допишешь в `files`.

Если ШАГ 1 обнаружил признаки другого типа специализации - в плане отдельным абзацем перечисли эти файлы и сообщи, что они НЕ удаляются (см. раздел МИГРАЦИЯ).

Жди "ок". Без подтверждения - не действуй.

ШАГ 4. Действуй (после "ок").

### 4a. Скопируй канонические rules и agents из `<canon_base>`

Для каждого файла из ШАГ 2 (A + B), которого еще нет в проекте:

- Если этот промт загружен по HTTP: `WebFetch <canon_base>/rules/<name>.md` -> запиши в `.claude/rules/<name>.md`. Аналогично для agents.
- Локально (если читался с диска): `cp <canon_base>/rules/<name>.md .claude/rules/<name>.md`. Аналогично для agents.

Существующие файлы НЕ перезаписываются. Для апгрейда к актуальной версии канона есть `migrations/sync-from-canon.prompt.md`.

### 4b. Создай `.claude/rules/wiki-structure.md` (если отсутствует)

```markdown
---
description: Структура vault'а - разделы, теги, конвенции конкретного проекта
---

# Структура vault'а

TODO: заполни структуру конкретного vault'а.

## Разделы (папки верхнего уровня)

- `inbox/` - входящие черновики, еще не разложенные по темам.
- TODO: перечисли тематические разделы (например, `велосипед/`, `здоровье/`, `путешествия/`).

## Теги

Иерархические через `/`. Один корневой тег на тематический домен:

- TODO: перечисли корневые теги и их подтеги (например, `велосипед/трансмиссия`, `велосипед/тормоза`).

Сквозные теги (статусы, типы):

- `draft` - черновик, не готово.
- `archive` - устарело, оставлено для истории.
- TODO: добавь свои сквозные теги, если нужны.

## MOC (map of content)

TODO: перечисли существующие MOC-заметки (если есть). Например: `велосипед-moc.md` - входная карта по велосипеду.

## Конвенции имени файла

TODO: латиница или кириллица; что делать с пробелами и регистром (по умолчанию из `wiki-notes-style.md` - kebab-case).
```

Это локальный файл, не из канона - каждый vault заполняет его сам. В `canon.yaml.local_only` не пишем (по умолчанию это понятно).

### 4c. Допиши `.claude/canon.yaml`

В секцию `files` добавь записи, которых там еще нет:

```yaml
  - rules/wiki-notes-style.md
  - rules/wiki-linking-obsidian.md
  - agents/note-writer.md
  - agents/librarian.md
```

Если `canon.yaml` нет - значит bootstrap-02 не выполнялся; сообщи об этом и не создавай `canon.yaml` сам (это работа шага 02).

Отдельно: если в `canon.yaml` поле `project_type` пустое (`""`) или отсутствует - проставь `project_type: wiki` на верхнем уровне. Если стоит другой тип (это случай **миграции** в wiki, см. раздел МИГРАЦИЯ ниже) - сообщи и спроси: переписать на `wiki` сейчас или оставить старый тип и переписать вручную после миграционных шагов.

### 4d. Дополни существующие файлы

Дополнения CLAUDE.md (ссылки `@.claude/rules/wiki-notes-style.md`, `@.claude/rules/wiki-linking-obsidian.md`, `@.claude/rules/wiki-structure.md`) - по отдельному "ок".

ШАГ 5. Отчет.

Таблица: файл / действие. Что осталось вручную (заполнить `wiki-structure.md`).

Если был миграционный кейс - напомни о разделе МИГРАЦИЯ ниже.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
МИГРАЦИЯ из другого типа в wiki
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Этот промт сам по себе только **добавляет** wiki-специализацию. Если проект раньше был другого типа и от старой специализации нужно избавиться - сделай это вручную **после** ШАГ 4:

1. Из `.claude/canon.yaml -> files` удали записи старого типа. Маппинг типа -> файлы:
   - **management** -> `rules/meetings.md`, `rules/tasks-tracking.md`, `rules/artifacts-structure.md`, `rules/meeting-transcripts.md`, `rules/name-cross-check.md`, `rules/google-sheets-mcp.md`, `rules/estimates-in-hours.md`, `agents/tracker.md`.
   - **education** -> `rules/lecture-notes.md`, `rules/homework.md`, `rules/course-structure.md`, `agents/note-taker.md`, `agents/tutor.md`.
   - **documentation** -> `agents/copy-editor.md`, и локально - `rules/style-guide.md`.
   - **coding** -> `rules/karpathy-guidelines.md`, `rules/tests-coverage.md`, `rules/error-exposure.md`, `agents/debugger.md`, `agents/implementer.md`, `agents/code-reviewer.md`.
   - **claude-tooling** -> `rules/prompt-conventions.md`, `agents/prompt-reviewer.md`.
2. Удали соответствующие файлы из `.claude/rules/` и `.claude/agents/` (если они не используются по другим причинам).
3. Из `CLAUDE.md` убери `@-`ссылки на удаленные правила.
4. Из `.claude/settings.json` убери allow-блоки старого типа (если bootstrap-03 старого типа их добавлял - например, генераторы статсайтов в documentation).
5. В `.claude/canon.yaml` смени `project_type` на `wiki` (если на ШАГ 4 ты оставил старое значение, см. вопрос в "Отдельно:" выше).

Актуальный список файлов по типам - в `<canon_base>/manifest.yaml`.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ПРАВИЛА БЕЗОПАСНОСТИ
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

- Без моего "ок" после плана - никаких изменений.
- Существующие файлы не перезаписываешь.
- Дополнения CLAUDE.md - только по отдельному "ок".
- Файлы старой специализации (при миграции) НЕ удаляешь автоматически - только инструкция в разделе МИГРАЦИЯ.

---

После завершения:

- Заполни `.claude/rules/wiki-structure.md` - разделы и теги конкретного vault'а.
- Для обновления канонических правил/агентов в будущем используй `migrations/sync-from-canon.prompt.md`.
