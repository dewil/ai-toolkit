# Промт: scaffold для кодовых проектов

Этот файл - инструкция для Claude Code. Запускать **после** `bootstrap-02-scaffold.prompt.md`: открой Claude Code в **корне проекта** и попроси выполнить этот файл (например: `выполни инструкции из <путь>/bootstrap-03-coding.prompt.md`).

Промт безопасен для существующих проектов. Сначала аудит, действия - после "ок". Существующие файлы не перезаписываются.

Добавляет к общему scaffold:

- агентов: `debugger`, `implementer`, `code-reviewer`
- правила: `karpathy-guidelines.md`, `tests-coverage.md`, `error-exposure.md`
- стек-специфичные allow в `.claude/settings.json` (по подтверждению, по списку)
- `@-`ссылки на `karpathy-guidelines.md`, `tests-coverage.md` и `error-exposure.md` в `CLAUDE.md`
- PostToolUse-хуки для авто-проверок (синтаксический lint после Edit/Write) - по подтверждению, на основе стека.

---

КОНТЕКСТ ЦЕПОЧКИ. Это шаг 03. **Источник этого файла** (URL или локальный путь) определяет базу для канонических файлов:

- `canon_base` = подняться на один уровень из `bootstrap/`. Например, если этот файл загружен по URL `.../main/bootstrap/bootstrap-03-coding.prompt.md`, то `canon_base = .../main/`.
- Канонические агенты и правила специализации: `<canon_base>/agents/*.md`, `<canon_base>/rules/*.md`.
- Предыдущий шаг (сиблинг): `bootstrap-02-scaffold.prompt.md` в той же папке.

Ты в корне кодового проекта. Раскатываешь специализацию scaffold под кодинг: code-агенты, правило про тесты, стек-специфичные разрешения. Базовый scaffold должен быть уже на месте (bootstrap-02-scaffold).

ВАЖНО. Промт безопасен для существующих проектов. Сначала аудит, потом план, потом ждешь "ок", только потом действуешь. Существующие файлы НЕ перезаписываешь.

ШАГ 1. Аудит (только чтение).

- pwd - корень проекта.
- Перечисли существующие:
  - .claude/agents/debugger.md, implementer.md, code-reviewer.md
  - .claude/rules/karpathy-guidelines.md, tests-coverage.md, error-exposure.md
  - .claude/canon.yaml - есть ли (должен быть после bootstrap-02), что в `files`
  - CLAUDE.md - какие @-ссылки уже есть
  - .claude/settings.json - какие allow уже есть (для сопоставления со стеком)
- Определи стек проекта по маркерам в корне (только маркеры, не глубокий анализ):
  - PHP/Laravel: composer.json, artisan, vendor/
  - Node/JS/TS: package.json, node_modules/
  - Python: pyproject.toml, requirements.txt, setup.py
  - Go: go.mod
  - Rust: Cargo.toml
  - другое: укажи, какие маркеры нашел
- Проверь, есть ли тестовая инфраструктура (vendor/bin/phpunit, jest.config*, pytest.ini, go test и т.п.).

ШАГ 2. Целевое состояние.

A. .claude/agents/ (источник - `<canon_base>/agents/`):
   - debugger.md
   - implementer.md
   - code-reviewer.md

B. .claude/rules/ (источник - `<canon_base>/rules/`):
   - karpathy-guidelines.md
   - tests-coverage.md
   - error-exposure.md

C. CLAUDE.md содержит ссылки `@.claude/rules/karpathy-guidelines.md`, `@.claude/rules/tests-coverage.md` и `@.claude/rules/error-exposure.md`.

D. .claude/canon.yaml -> `files` содержит 6 файлов специализации (agents/debugger.md, agents/implementer.md, agents/code-reviewer.md, rules/karpathy-guidelines.md, rules/tests-coverage.md, rules/error-exposure.md).

E. .claude/settings.json дополнен стек-специфичными allow по результату аудита (предложение, по подтверждению).

F. .claude/settings.json содержит секцию `hooks` с PostToolUse-проверками для стека (php -l для PHP, node --check для JS, и т.п.). Шаблоны - ниже. Применяются по подтверждению.

ШАГ 3. План.

Таблица: файл / действие / статус. Для существующих - "не трону". Для отсутствующих - "создам".

Отдельно покажи список стек-специфичных allow, которые предлагаешь добавить в settings.json, исходя из аудита (см. ниже шаблоны). НЕ применяй без отдельного "ок".

Для CLAUDE.md - если файла нет или нет ссылок `@.claude/rules/karpathy-guidelines.md` / `@.claude/rules/tests-coverage.md` / `@.claude/rules/error-exposure.md` - покажи, что добавишь.

Для canon.yaml - покажи, какие 6 записей допишешь в `files`.

Жди "ок". Без подтверждения - не действуй.

ШАГ 4. Действуй (после "ок").

### 4a. Скопируй канонические агенты и правило из `<canon_base>`

Для каждого файла из ШАГ 2 (A + B), которого еще нет в проекте:

- Если этот промт загружен по HTTP: `WebFetch <canon_base>/agents/<name>.md` -> запиши в `.claude/agents/<name>.md`. Аналогично для каждого `<canon_base>/rules/<name>.md` -> `.claude/rules/<name>.md`.
- Локально (если читался с диска): `cp <canon_base>/agents/<name>.md .claude/agents/<name>.md`. Аналогично для rules.

Существующие файлы НЕ перезаписываются. Для апгрейда к актуальной версии канона есть `migrations/sync-from-canon.prompt.md`.

### 4b. Допиши `.claude/canon.yaml`

В секцию `files` добавь записи, которых там еще нет:

```yaml
  - agents/debugger.md
  - agents/implementer.md
  - agents/code-reviewer.md
  - rules/karpathy-guidelines.md
  - rules/tests-coverage.md
  - rules/error-exposure.md
```

Если `canon.yaml` нет - значит bootstrap-02 не выполнялся; сообщи об этом и не создавай `canon.yaml` сам (это работа шага 02).

### 4c. Дополни существующие файлы

Дополнения CLAUDE.md (ссылки `@.claude/rules/karpathy-guidelines.md`, `@.claude/rules/tests-coverage.md`, `@.claude/rules/error-exposure.md`) и settings.json (allow, hooks) - по отдельному "ок" на каждый пункт.

ШАГ 5. Отчет.

Таблица: файл / действие. Список агентов и правил, готовых к использованию. Что осталось вручную.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ШАБЛОНЫ allow ДЛЯ settings.json (по стеку)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Предложи добавить в .claude/settings.json блок permissions.allow только для тех команд, которые соответствуют стеку из аудита. Не добавляй ничего без "ок".

PHP / Laravel:
- "Bash(php artisan *)"
- "Bash(php -v)"
- "Bash(./vendor/bin/phpunit *)"
- "Bash(vendor/bin/phpunit *)"
- "Bash(composer install*)"
- "Bash(composer require*)"
- "Bash(composer dump-autoload*)"

Node / JS / TS:
- "Bash(npm run *)"
- "Bash(npm install*)"
- "Bash(npx *)"
- "Bash(pnpm *)"
- "Bash(yarn *)"
- "Bash(node *)"

Python:
- "Bash(python *)"
- "Bash(python3 *)"
- "Bash(pip install*)"
- "Bash(pytest *)"
- "Bash(uv *)"
- "Bash(poetry *)"

Go:
- "Bash(go build*)"
- "Bash(go test*)"
- "Bash(go vet*)"
- "Bash(go run*)"

Rust:
- "Bash(cargo build*)"
- "Bash(cargo test*)"
- "Bash(cargo run*)"
- "Bash(cargo clippy*)"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ШАБЛОНЫ hooks ДЛЯ settings.json (по стеку)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Предложи добавить в .claude/settings.json блок `hooks` с проверками для стека из аудита. Это PostToolUse-хуки: после Edit/Write по подходящему файлу запускается синтаксический lint, и если ломает синтаксис - Claude увидит ошибку и исправит.

Не добавляй без "ок". Применяй только хуки под обнаруженный стек.

PHP - синтаксис php -l после Edit/Write на *.php:

```json
"hooks": {
  "PostToolUse": [
    {
      "matcher": "Edit|Write",
      "hooks": [
        {
          "type": "command",
          "command": "if [[ \"$CLAUDE_FILE_PATH\" == *.php ]]; then php -l \"$CLAUDE_FILE_PATH\"; fi"
        }
      ]
    }
  ]
}
```

Node/JS/TS - node --check для .js, tsc --noEmit для .ts (если есть tsconfig):

```json
"hooks": {
  "PostToolUse": [
    {
      "matcher": "Edit|Write",
      "hooks": [
        {
          "type": "command",
          "command": "case \"$CLAUDE_FILE_PATH\" in *.js|*.mjs) node --check \"$CLAUDE_FILE_PATH\";; *.ts|*.tsx) [ -f tsconfig.json ] && npx tsc --noEmit -p . ;; esac"
        }
      ]
    }
  ]
}
```

Python - `python -m py_compile`:

```json
{
  "type": "command",
  "command": "if [[ \"$CLAUDE_FILE_PATH\" == *.py ]]; then python3 -m py_compile \"$CLAUDE_FILE_PATH\"; fi"
}
```

Go - `go vet ./...` после Edit/Write на *.go:

```json
{
  "type": "command",
  "command": "if [[ \"$CLAUDE_FILE_PATH\" == *.go ]]; then go vet ./...; fi"
}
```

Rust - `cargo check` после Edit/Write на *.rs:

```json
{
  "type": "command",
  "command": "if [[ \"$CLAUDE_FILE_PATH\" == *.rs ]]; then cargo check; fi"
}
```

Если в проекте уже есть линтеры в CI (eslint, phpstan, ruff, clippy) - не дублируй их в хуках без подтверждения, такие проверки тяжелее и должны быть осознанным выбором. По умолчанию ставь только синтаксическую проверку.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ПРОЕКТНЫЕ ПРАВИЛА (по запросу)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Если у проекта есть свои стандарты, не покрытые общим scaffold - заведи отдельный mdc-файл. Примеры:

- `format-dimensions.md` - единый формат вывода размерностей (например, "2.50 min left", "1.20 Gb", с конкретными токенами sec/min/hour/Gb/Tb и числом знаков после точки). Полезно, если в проекте есть UI с показателями времени или трафика.
- `api-contracts.md` - правила именования endpoints, формат ошибок, версионирование.
- `commit-convention.md` - формат сообщений коммитов, ссылки на тикеты.
- `migration-rules.md` - правила миграций БД (NOT NULL только с дефолтом, бэкфилл отдельно от alter, и т.п.).

В общий scaffold они не идут - заводи только по явной просьбе пользователя или после консультации.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ПРАВИЛА БЕЗОПАСНОСТИ
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

- Без моего "ок" после плана - никаких изменений.
- Существующие файлы не перезаписываешь.
- Дополнения CLAUDE.md и settings.json - только по отдельному "ок".

---

После завершения:

- Заполни `.claude/rules/project-structure.md` деревом папок проекта.
- Если есть проектные форматы (форматы вывода, специфичные команды) - заведи отдельный `.claude/rules/<имя>.md`.
