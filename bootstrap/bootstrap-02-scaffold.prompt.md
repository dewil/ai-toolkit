# Промт: общий scaffold (для проектов любого типа)

Этот файл - шаг 02 в bootstrap-цепочке claude-toolkit. Запускать **после** `bootstrap-01-memory.prompt.md`.

Промт безопасен для существующих проектов: сначала аудит, действия - после твоего "ок". Существующие файлы не перезаписываются.

После общей части - запускается специализированный промт под тип проекта (`bootstrap-03-*.prompt.md`).

---

КОНТЕКСТ ЦЕПОЧКИ. Это шаг 02. **Источник этого файла** (URL или локальный путь) определяет базу для соседей:

- `canon_base` = подняться на один уровень из `bootstrap/`. Например, если этот файл загружен по URL `.../main/bootstrap/bootstrap-02-scaffold.prompt.md`, то `canon_base = .../main/`.
- Канонические файлы правил, агентов и скиллов: `<canon_base>/rules/*.md`, `<canon_base>/agents/*.md`, `<canon_base>/skills/<name>/`.
- Предыдущий шаг (сиблинг): `bootstrap-01-memory.prompt.md` в той же папке.
- Специализация (сиблинги в той же папке): `bootstrap-03-{coding,management,education,documentation,claude-tooling,wiki}.prompt.md`.

Ты в корне проекта. Раскатываешь общий scaffold, нужный любому проекту независимо от типа: базовые правила, базовые агенты, settings.json, CLAUDE.md, canon.yaml. Это слой 2; специализация под тип проекта - отдельным промтом `bootstrap-03-*`.

ВАЖНО. Промт безопасен для существующих проектов. Сначала аудит, потом план, потом ждешь "ок", только потом действуешь. Существующие файлы НЕ перезаписываешь - только дополняешь по согласованию.

## ШАГ 1. Аудит (только чтение)

- pwd -> должна быть корнем проекта (наличие .claude/ или CLAUDE.md или .git/).
- Перечисли существующие:
  - .claude/agents/*.md
  - .claude/rules/*.md
  - .claude/skills/*/ (папки скиллов)
  - .claude/settings.json (если есть - прочти и покажи блоки allow/deny)
  - .claude/settings.local.json (если есть - не показывай содержимое, только факт наличия)
  - .claude/canon.yaml (если есть - покажи содержимое: где канон, какие файлы отслеживаются, есть ли upstream_pending)
  - CLAUDE.md (если есть - прочти и покажи список @-ссылок на правила)
  - .gitignore (если есть - проверь наличие записей `.claude/settings.local.json` и `.claude/memory/`)

## ШАГ 2. Целевое состояние

После общего scaffold должно быть:

**A. Канонические агенты в `.claude/agents/`** (источник - `<canon_base>/agents/`):

- architect.md, explorer.md, searcher.md, planner.md, summarizer.md

**B. Канонические правила в `.claude/rules/`** (источник - `<canon_base>/rules/`):

- typography-ru.md, subagents-usage.md, docs-maintenance.md
- project-structure.md - локально создаваемый шаблон с TODO (содержимое в ШАГ 4c)

`karpathy-guidelines.md` намеренно не идет в общий scaffold - он специфичен для кодинга (упоминает тесты, рефакторинг, "LLM coding mistakes") и подключается в `bootstrap-03-coding`.

**C. Канонические скиллы в `.claude/skills/`** (источник - `<canon_base>/skills/`):

- codex-audit/ - скилл делегирования аудита в Codex CLI (read-only). Скилл - это папка; список ее файлов берется из `canon.yaml`. У `codex-audit` это один файл `skills/codex-audit/SKILL.md`.

**D. `.claude/canon.yaml`** - метаданные канона, см. шаблон в ШАГ 4b.

**E. `.claude/settings.json`** с базовыми allow и расширенным deny (шаблон в ШАГ 4d).

**F. `CLAUDE.md`** со ссылками `@.claude/rules/*.md` на каноничные правила + `project-structure.md` (шаблон в ШАГ 4e).

**G.** В `.gitignore` уже должен быть `.claude/settings.local.json` (добавлен в start.md). Если нет - зафиксируй и добавь по отдельному "ок".

## ШАГ 3. План

Сравни целевое с фактическим. Для каждого пункта - статус и действие:

- "отсутствует, создам" - перечисли с буквальным путем.
- "есть, не трону" - перечисли (например, `.claude/rules/subagents-usage.md` уже есть, не перезаписываем).
- "есть, могу дополнить" - для CLAUDE.md перечисли отсутствующие @-ссылки; для settings.json перечисли отсутствующие базовые deny-правила; для .gitignore перечисли отсутствующие записи. Дополнения предлагаешь, но НЕ применяешь без отдельного "ок".

Покажи план таблицей: `файл | статус | действие | источник`. Жди "ок". Без подтверждения - не действуй.

## ШАГ 4. Действуй (только после "ок")

### 4a. Скопируй канонические rules, agents и skills из `<canon_base>`

Для каждого файла из ШАГ 2 (A + B), которого еще нет в проекте:

- Если этот промт загружен по HTTP: `WebFetch <canon_base>/rules/<name>.md` -> запиши в `.claude/rules/<name>.md`. Аналогично для agents.
- Локально (если читался с диска): `cp <canon_base>/rules/<name>.md .claude/rules/<name>.md`. Аналогично для agents.

Скиллы (ШАГ 2 C) - это папки. Каждый файл скилла копируется тем же способом (WebFetch/cp) в `.claude/skills/<name>/`, папку при необходимости создай. Список файлов скилла бери из шаблона `canon.yaml` ниже - для `codex-audit` это один `skills/codex-audit/SKILL.md`.

Существующие файлы НЕ перезаписываются (это случай "уже было"). Для апгрейда к актуальной версии канона есть отдельный промт `migrations/sync-from-canon.prompt.md`.

### 4b. Создай `.claude/canon.yaml`

```yaml
project_type: ""      # будет проставлен bootstrap-03-* (coding | management | education | documentation | claude-tooling | wiki)
canon:
  repo: https://github.com/dewil/claude-toolkit
  raw_base: https://raw.githubusercontent.com/dewil/claude-toolkit/main
  branch: main
  bootstrapped_at: <ISO-дата сегодня>
files:
  - rules/typography-ru.md
  - rules/subagents-usage.md
  - rules/docs-maintenance.md
  - agents/architect.md
  - agents/explorer.md
  - agents/searcher.md
  - agents/planner.md
  - agents/summarizer.md
  - skills/codex-audit/SKILL.md
local_only: []        # файлы в проекте, которых нет в каноне (свои правила)
skip_sync: []         # есть в каноне, но проект сознательно не накатывает обновления
upstream_pending: []  # файлы, помеченные к выносу в канон (см. sync-from-canon.prompt.md)
```

Поле `project_type` пока пустое - его проставит специализированный `bootstrap-03-*` на ШАГ 6. Это поле читает `sync-from-canon.prompt.md` для autodiscovery новых канон-файлов своего типа.

Если в проекте уже был `canon.yaml` - НЕ перезаписывай, оставь как есть. Если bootstrap-цепочка запускается повторно на уже-настроенном проекте, актуализация канона делается через `sync-from-canon.prompt.md`.

### 4c. Создай `.claude/rules/project-structure.md` из шаблона

Если файла еще нет в проекте:

- Если этот промт загружен по HTTP: `WebFetch <canon_base>/templates/project-structure.md` -> запиши в `.claude/rules/project-structure.md`.
- Локально (если читался с диска): `cp <canon_base>/templates/project-structure.md .claude/rules/project-structure.md`.

Существующий файл НЕ перезаписывай.

Это шаблон, а не каноническое правило - после копирования проект владеет файлом сам, sync его не контролирует, в `canon.yaml.files` его НЕ записываем. В `canon.yaml.local_only` тоже не пишем (по умолчанию это понятно).

### 4d. Создай `.claude/settings.json` (если отсутствует)

Шаблон ниже содержит плейсхолдер `<RAW_BASE>` - подставь туда значение `canon.raw_base` из `.claude/canon.yaml`, который ты создал в шаге 4b (например, `https://raw.githubusercontent.com/dewil/claude-toolkit/main`). Это нужно, чтобы auto mode не блокировал фетчи канона при последующих синках через `sync-from-canon.prompt.md`.

```json
{
  "permissions": {
    "allow": [
      "Read(**)",
      "Edit(**)",
      "Write(**)",
      "Bash(ls *)",
      "Bash(git diff*)",
      "Bash(git status)",
      "Bash(git log*)",
      "Bash(codex exec -s read-only:*)",
      "Bash(curl -fsSL <RAW_BASE>/*)",
      "WebFetch(domain:raw.githubusercontent.com)"
    ],
    "deny": [
      "Bash(rm -rf *)",
      "Bash(chmod 777 *)",
      "Bash(chmod -R 777*)",
      "Bash(chmod -R 666*)",
      "Bash(chown -R *)",
      "Bash(find * -delete*)",
      "Bash(find * -exec rm*)",
      "Bash(xargs rm*)",
      "Bash(git push --force*)",
      "Bash(git push -f*)"
    ]
  }
}
```

Если `.claude/settings.json` уже существует - не перезаписывай. По отдельному "ок" допиши недостающие записи в `allow` (особенно `Bash(curl -fsSL <RAW_BASE>/*)` и `WebFetch(domain:raw.githubusercontent.com)`, если их нет).

### 4e. Создай или дополни `CLAUDE.md`

```markdown
Общие поведенческие правила (применяются всегда):

@.claude/rules/subagents-usage.md
@.claude/rules/docs-maintenance.md
@.claude/rules/project-structure.md
@.claude/rules/typography-ru.md
```

Если CLAUDE.md уже есть - дополни недостающими @-ссылками (по отдельному "ок"), не перезаписывай весь файл.

## ШАГ 5. Отчет

Таблица: `файл | действие` (создан / уже существовал - не тронут / дополнен по согласованию / пропущен).

Отдельно: какие файлы требуют ручного заполнения (`project-structure.md` - дерево папок проекта).

Отдельно: `.claude/canon.yaml` - подтверди, что URL канона корректный и `files` соответствует тому, что реально скопировано.

## ШАГ 6. Цепочка - запуск bootstrap-03-* по типу проекта

После отчета спроси одной строкой:

> Какой тип проекта - запустить соответствующий bootstrap-03-*? 1) кодинг 2) управление 3) учеба 4) документация 5) claude-tooling 6) wiki 7) пропустить

Маппинг ответа -> имя файла-сиблинга в той же папке `bootstrap/`:

- 1 / кодинг / coding -> `bootstrap-03-coding.prompt.md`
- 2 / управление / management -> `bootstrap-03-management.prompt.md`
- 3 / учеба / education -> `bootstrap-03-education.prompt.md`
- 4 / документация / docs / documentation -> `bootstrap-03-documentation.prompt.md`
- 5 / claude / claude-tooling / tooling -> `bootstrap-03-claude-tooling.prompt.md`
- 6 / wiki / вики / obsidian / knowledge-base -> `bootstrap-03-wiki.prompt.md`
- 7 / пропустить / нет / skip -> остановись, ничего не делай.

Если выбран один из 1-6:

- Следующий файл - сиблинг в той же папке: `<dirname(этого файла)>/<имя файла>`. По HTTP - WebFetch, локально - Read.
- Содержимое файла - это инструкции для тебя. Выполни их как прямое продолжение текущей сессии (не пересылай пользователю, не цитируй - именно выполни). Все правила безопасности из 03 действуют.

## ПРАВИЛА БЕЗОПАСНОСТИ

- Без моего "ок" после плана - никаких изменений.
- Существующие файлы не перезаписываешь.
- Дополнения существующих файлов (CLAUDE.md, settings.json, .gitignore) - только по отдельному "ок" на каждый пункт.
- `canon.yaml`, если уже есть, не пересоздаешь - актуализация через `migrations/sync-from-canon.prompt.md`.
- Никаких `rm -rf`.

---

После завершения:

- Заполни `.claude/rules/project-structure.md` деревом папок проекта.
- Запусти специализированный промт под тип проекта.
- Стек/тип-специфичные allow в `.claude/settings.json` (например, `Bash(php artisan *)`, `Bash(npm run *)`) добавятся специализированным промтом или вручную.
- Для обновления канонических правил/агентов в будущем используй `migrations/sync-from-canon.prompt.md`.
