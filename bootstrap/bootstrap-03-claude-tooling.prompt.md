# Промт: scaffold для claude-tooling проектов

Этот файл - инструкция для Claude Code. Запускать **после** `bootstrap-02-scaffold.prompt.md`: открой Claude Code в **корне проекта** и попроси выполнить этот файл (например: `выполни инструкции из <путь>/bootstrap-03-claude-tooling.prompt.md`).

Промт безопасен для существующих проектов. Сначала аудит, действия - после "ок". Существующие файлы не перезаписываются.

claude-tooling проект - тот, чей артефакт это сами промты, агенты, правила, канон для работы Claude (как репозиторий `claude-toolkit`).

Добавляет к общему scaffold:

- агента: `prompt-reviewer` (ревью промтов на ясность, безопасность, идемпотентность)
- правило: `prompt-conventions.md` (идемпотентность, audit-plan-ok-action, sibling-paths, единый источник)
- `@-`ссылку на `prompt-conventions.md` в `CLAUDE.md`
- allow для тестового фетча своих промтов в `.claude/settings.json` (по подтверждению)

---

КОНТЕКСТ ЦЕПОЧКИ. Это шаг 03. **Источник этого файла** (URL или локальный путь) определяет базу для канонических файлов:

- `canon_base` = подняться на один уровень из `bootstrap/`. Например, если этот файл загружен по URL `.../main/bootstrap/bootstrap-03-claude-tooling.prompt.md`, то `canon_base = .../main/`.
- Канонические агенты и правила специализации: `<canon_base>/agents/*.md`, `<canon_base>/rules/*.md`.
- Предыдущий шаг (сиблинг): `bootstrap-02-scaffold.prompt.md` в той же папке.

Ты в корне claude-tooling проекта. Раскатываешь специализацию scaffold: агент-ревьюер промтов, правило конвенций промтов, allow под тестовый фетч. Базовый scaffold должен быть уже на месте (bootstrap-02-scaffold).

ВАЖНО. Промт безопасен для существующих проектов. Сначала аудит, потом план, потом ждешь "ок", только потом действуешь. Существующие файлы НЕ перезаписываешь.

ШАГ 1. Аудит (только чтение).

- pwd - корень проекта.
- Перечисли существующие:
  - .claude/agents/prompt-reviewer.md
  - .claude/rules/prompt-conventions.md
  - .claude/canon.yaml - есть ли (должен быть после bootstrap-02), что в `files`
  - CLAUDE.md - какие @-ссылки уже есть
  - .claude/settings.json - какие allow уже есть

ШАГ 2. Целевое состояние.

A. .claude/agents/ (источник - `<canon_base>/agents/`):
   - prompt-reviewer.md

B. .claude/rules/ (источник - `<canon_base>/rules/`):
   - prompt-conventions.md

C. CLAUDE.md содержит ссылку `@.claude/rules/prompt-conventions.md`.

D. .claude/canon.yaml -> `files` содержит 2 файла специализации (agents/prompt-reviewer.md, rules/prompt-conventions.md).

E. .claude/settings.json дополнен allow для тестового фетча своих промтов (предложение, по подтверждению).

ШАГ 3. План.

Таблица: файл / действие / статус. Для существующих - "не трону". Для отсутствующих - "создам".

Отдельно покажи список allow, которые предлагаешь добавить в settings.json (см. ниже шаблоны). НЕ применяй без отдельного "ок".

Для CLAUDE.md - если файла нет или нет ссылки `@.claude/rules/prompt-conventions.md` - покажи, что добавишь.

Для canon.yaml - покажи, какие 2 записи допишешь в `files`.

Жди "ок". Без подтверждения - не действуй.

ШАГ 4. Действуй (после "ок").

### 4a. Скопируй канонического агента и правило из `<canon_base>`

Для каждого файла из ШАГ 2 (A + B), которого еще нет в проекте:

- Если этот промт загружен по HTTP: `WebFetch <canon_base>/agents/prompt-reviewer.md` -> запиши в `.claude/agents/prompt-reviewer.md`. Аналогично `<canon_base>/rules/prompt-conventions.md` -> `.claude/rules/prompt-conventions.md`.
- Локально (если читался с диска): `cp <canon_base>/agents/prompt-reviewer.md .claude/agents/prompt-reviewer.md`. Аналогично для rules.

Существующие файлы НЕ перезаписываются. Для апгрейда к актуальной версии канона есть `migrations/sync-from-canon.prompt.md`.

### 4b. Допиши `.claude/canon.yaml`

В секцию `files` добавь записи, которых там еще нет:

```yaml
  - agents/prompt-reviewer.md
  - rules/prompt-conventions.md
```

Если `canon.yaml` нет - значит bootstrap-02 не выполнялся; сообщи об этом и не создавай `canon.yaml` сам (это работа шага 02).

### 4c. Дополни существующие файлы

Дополнения CLAUDE.md (ссылка `@.claude/rules/prompt-conventions.md`) и settings.json (allow) - по отдельному "ок" на каждый пункт.

ШАГ 5. Отчет.

Таблица: файл / действие. Список агентов и правил, готовых к использованию. Что осталось вручную.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ШАБЛОНЫ allow ДЛЯ settings.json
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Предложи добавить в .claude/settings.json блок permissions.allow. Не добавляй ничего без "ок".

Тестовый фетч своих промтов по HTTP (проверить, как промт грузится из канона):
- "Bash(curl -s https://raw.githubusercontent.com/*)"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ПРАВИЛА БЕЗОПАСНОСТИ
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

- Без моего "ок" после плана - никаких изменений.
- Существующие файлы не перезаписываешь.
- Дополнения CLAUDE.md и settings.json - только по отдельному "ок".

---

После завершения:

- Если этот проект сам является источником канона для других проектов - образцом раскладки служит репозиторий `claude-toolkit` (точка входа `start.md`, bootstrap-цепочка, плоские `rules/`/`agents/`/`skills/`).
- Для обновления канонических агентов/правил в будущем используй `migrations/sync-from-canon.prompt.md`.
