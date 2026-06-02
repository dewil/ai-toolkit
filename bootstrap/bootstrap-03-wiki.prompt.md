# Промт: scaffold для wiki-проектов (Obsidian)

Этот файл - инструкция для Claude Code. Запускать **после** `bootstrap-02-scaffold.prompt.md`: открой Claude Code в **корне vault'а** и попроси выполнить этот файл (например: `выполни инструкции из <путь>/bootstrap-03-wiki.prompt.md`).

Промт безопасен для существующих проектов. Сначала аудит, действия - после "ок". Существующие файлы не перезаписываются.

Подходит и для **добавления wiki-специализации** к уже бутстрапленному проекту другого типа (например, `management`) - получится мультиспециализированный проект (`project_type: [management, wiki]`). Промт только добавляет файлы wiki-специализации и не трогает существующее, файлы других типов не удаляет. Если нужна **замена** старого типа на wiki (а не добавление поверх) - см. раздел МИГРАЦИЯ в конце файла.

Добавляет к общему scaffold:

- правила: `wiki-notes-style.md` (формат заметки), `wiki-linking-obsidian.md` (правила `[[ссылок]]`)
- агентов: `note-writer` (создает/правит заметки), `librarian` (read-only ревизия vault'а)
- скилл `telegram-snapshot-setup` - первая настройка автоматического pull чатов Telegram (грабли my.telegram.org, PeerUser, fallback на публичные ключи tdesktop); тот же скилл подключен у management, в мультиспециализированных проектах копируется один раз.
- скрипты в корне vault'а:
  - `scripts/telegram-snapshot.py` - инкрементальный pull новых сообщений с тремя режимами (bootstrap / migration / incremental).
  - `scripts/telegram-deltas.py` - расчет дельт между текущим и предыдущим snapshot, удобно для дневных сводок.
- шаблон `rules/wiki-structure.md` (структура разделов, теги конкретного vault'а - копируется один раз, дальше заполняется проектом)
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
  - .claude/skills/telegram-snapshot-setup/SKILL.md
  - scripts/telegram-snapshot.py, scripts/telegram-deltas.py
  - .claude/canon.yaml - есть ли (должен быть после bootstrap-02), что в `files`
  - CLAUDE.md - какие @-ссылки уже есть
- Если в `.claude/canon.yaml -> project_type` уже есть другие типы (например, `[management]`, `[coding, documentation]`) или в `files` уже есть файлы другой специализации - это **мультиспециализированный проект**. Wiki-специализация будет ДОБАВЛЕНА поверх, существующее не удаляется. Сообщи в плане: "Проект уже имеет типы [<список>]. Wiki добавится в `project_type` и в `files`; старые файлы остаются". Если пользователь хотел не добавить wiki, а ЗАМЕНИТЬ старый тип целиком - см. раздел МИГРАЦИЯ в конце файла.

ШАГ 2. Целевое состояние.

A. .claude/rules/ (источник - `<canon_base>/rules/`):
   - wiki-notes-style.md
   - wiki-linking-obsidian.md

B. .claude/agents/ (источник - `<canon_base>/agents/`):
   - note-writer.md
   - librarian.md

C. .claude/skills/ (источник - `<canon_base>/skills/`):
   - telegram-snapshot-setup/SKILL.md - один файл в папке скилла.

D. scripts/ в корне vault'а (источник - `<canon_base>/scripts/`):
   - telegram-snapshot.py
   - telegram-deltas.py

E. .claude/rules/:
   - wiki-structure.md - копируется из `<canon_base>/templates/wiki-structure.md` (см. ШАГ 4b). Это шаблон, не каноническое правило: разделы и теги у каждого vault'а свои, sync его не контролирует.

F. CLAUDE.md содержит ссылки `@.claude/rules/wiki-notes-style.md`, `@.claude/rules/wiki-linking-obsidian.md`, `@.claude/rules/wiki-structure.md`.

G. `.claude/canon.yaml`:
   - `files` содержит файлы wiki-специализации (A + B + C + D).
   - `project_type` - список, и `wiki` в нем (если списка нет/пустой - инициализируется на ШАГ 4; если `wiki` отсутствует среди других типов - добавляется на ШАГ 4).

ШАГ 3. План.

Таблица: файл / действие / статус. Для существующих - "не трону". Для отсутствующих - "создам".

Для CLAUDE.md - если файла нет или нет нужных @-ссылок - покажи, что добавишь.

Для canon.yaml - покажи, какие записи допишешь в `files`.

Если ШАГ 1 обнаружил признаки другого типа специализации - в плане отдельным абзацем перечисли эти типы/файлы и сообщи: "Проект становится мультиспециализированным; старые файлы не удаляются. Если хотел не добавить wiki, а ЗАМЕНИТЬ старый тип целиком - см. раздел МИГРАЦИЯ".

Жди "ок". Без подтверждения - не действуй.

ШАГ 4. Действуй (после "ок").

### 4a. Скопируй канонические rules, agents, скилл и скрипты из `<canon_base>`

Для каждого файла из ШАГ 2 (A + B + C + D), которого еще нет в проекте:

- Если этот промт загружен по HTTP:
  - `WebFetch <canon_base>/rules/<имя>.md` -> запиши в `.claude/rules/<имя>.md`.
  - `WebFetch <canon_base>/agents/<имя>.md` -> `.claude/agents/<имя>.md`.
  - `WebFetch <canon_base>/skills/telegram-snapshot-setup/SKILL.md` -> `.claude/skills/telegram-snapshot-setup/SKILL.md` (папку при необходимости создай).
  - `WebFetch <canon_base>/scripts/<имя>.py` -> `scripts/<имя>.py` в корне vault'а (папку при необходимости создай). Сохрани executable-бит через `chmod +x scripts/<имя>.py`.
- Локально (если читался с диска): `cp <canon_base>/<тот же относительный путь> <тот же таргет>`. Для скриптов отдельно `chmod +x scripts/<имя>.py`.

Существующие файлы НЕ перезаписываются. Для апгрейда к актуальной версии канона (включая скрипты с прогревом и migration-логикой) есть `migrations/sync-from-canon.prompt.md`.

### 4b. Создай `.claude/rules/wiki-structure.md` из шаблона

Если файла еще нет в проекте:

- Если этот промт загружен по HTTP: `WebFetch <canon_base>/templates/wiki-structure.md` -> запиши в `.claude/rules/wiki-structure.md`.
- Локально (если читался с диска): `cp <canon_base>/templates/wiki-structure.md .claude/rules/wiki-structure.md`.

Существующий файл НЕ перезаписывай.

Это шаблон, а не каноническое правило - после копирования vault владеет файлом сам, sync его не контролирует, в `canon.yaml.files` его НЕ записываем. В `canon.yaml.local_only` тоже не пишем (по умолчанию это понятно).

### 4c. Допиши `.claude/canon.yaml`

В секцию `files` добавь записи, которых там еще нет:

```yaml
  - rules/wiki-notes-style.md
  - rules/wiki-linking-obsidian.md
  - agents/note-writer.md
  - agents/librarian.md
  - skills/telegram-snapshot-setup/SKILL.md
  - scripts/telegram-snapshot.py
  - scripts/telegram-deltas.py
```

В мультиспециализированных проектах (например, wiki+management) часть этих записей могла уже появиться от предыдущего bootstrap-03 - не дублируй, просто пропусти то, что уже есть в `files`.

Если `canon.yaml` нет - значит bootstrap-02 не выполнялся; сообщи об этом и не создавай `canon.yaml` сам (это работа шага 02).

Отдельно про `project_type` (всегда **список**):

- Если поля нет вообще или это пустой список `[]` - инициализируй `project_type: [wiki]`.
- Если поле уже содержит `wiki` - не трогай (идемпотентность).
- Если поле содержит другие типы (`[management]`, `[coding, documentation]` и т.п.), но без `wiki` - **добавь** `wiki` в конец списка (`[management] -> [management, wiki]`). Это нормальный случай мультиспециализации; не "миграция типа", не спрашивай - добавляй. Если пользователь хочет ЗАМЕНИТЬ старый тип на wiki - см. раздел МИГРАЦИЯ.
- Если поле - скаляр-строка (`project_type: management`, старый формат до перехода на список) - перепиши в список и добавь `wiki`: `[management, wiki]`. Сообщи в отчете о миграции формата.

### 4d. Дополни существующие файлы

Дополнения CLAUDE.md (ссылки `@.claude/rules/wiki-notes-style.md`, `@.claude/rules/wiki-linking-obsidian.md`, `@.claude/rules/wiki-structure.md`) - по отдельному "ок".

ШАГ 5. Отчет.

Таблица: файл / действие. Что осталось вручную (заполнить `wiki-structure.md`).

Если был миграционный кейс - напомни о разделе МИГРАЦИЯ ниже.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
МИГРАЦИЯ: ЗАМЕНА старого типа на wiki
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Этот промт сам по себе только **добавляет** wiki-специализацию - проект становится мультиспециализированным (`[<старый_тип>, wiki]`), что обычно и нужно. Этот раздел применяется только если пользователь хочет ЦЕЛИКОМ заменить старую специализацию на wiki (старая больше не нужна). Тогда **после** ШАГ 4:

1. Из `.claude/canon.yaml -> files` удали записи старого типа. Маппинг типа -> файлы:
   - **management** -> `rules/meetings.md`, `rules/tasks-tracking.md`, `rules/artifacts-structure.md`, `rules/meeting-transcripts.md`, `rules/name-cross-check.md`, `rules/google-sheets-mcp.md`, `rules/estimates-in-hours.md`, `agents/tracker.md`. **НЕ** удалять `skills/telegram-snapshot-setup/SKILL.md`, `scripts/telegram-snapshot.py`, `scripts/telegram-deltas.py` - они расшарены с wiki-секцией и продолжают использоваться.
   - **education** -> `rules/lecture-notes.md`, `rules/homework.md`, `rules/course-structure.md`, `agents/note-taker.md`, и `skills/tutor/SKILL.md` (если подключался).
   - **documentation** -> `agents/copy-editor.md`, и локально - `rules/style-guide.md`.
   - **coding** -> `rules/karpathy-guidelines.md`, `rules/tests-coverage.md`, `rules/error-exposure.md`, `agents/debugger.md`, `agents/implementer.md`, `agents/code-reviewer.md`.
   - **claude-tooling** -> `rules/prompt-conventions.md`, `agents/prompt-reviewer.md`.
2. Удали соответствующие файлы из `.claude/rules/` и `.claude/agents/` (если они не используются по другим причинам).
3. Из `CLAUDE.md` убери `@-`ссылки на удаленные правила.
4. Из `.claude/settings.json` убери allow-блоки старого типа (если bootstrap-03 старого типа их добавлял - например, генераторы статсайтов в documentation).
5. В `.claude/canon.yaml -> project_type` (списке) удали записи старого типа, оставь только `wiki`. Например, `[management, wiki] -> [wiki]`.

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
