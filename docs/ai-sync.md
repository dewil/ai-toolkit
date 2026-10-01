# Обновление канона в установленном .AI

Для первого обновления старого `.AI` навык может отсутствовать, а установленный `/canon` - останавливаться. Прямой HTTP-вход без предварительной установки навыка: "Выполни https://raw.githubusercontent.com/dewil/claude-toolkit/main/migrations/ai-sync.prompt.md для этого клиента". Для форка используй выбранный OWNER/REPO, другой ref задается явно. URL main служит discovery: до plan/apply процедура разрешает его в SHA и продолжает по закрепленной копии.

После обновления навык [canon-sync](../skills/canon-sync/SKILL.md) или новый `/canon` обновляет канон существующего клиента. [Исполнительная процедура](../migrations/ai-sync.prompt.md) задает audit, read-only plan, согласованное apply и check. Legacy `.claude` сохраняет прежний маршрут; переход в `.AI` выполняется только отдельной миграцией.

## Версия и запуск

HTTP-репозиторий берется из явно выбранного источника, иначе HTTPS `state.source.base`. Сохраненный SHA обозначает установленную версию; обычное обновление выбирает `main`, другой ref - по запросу пользователя. Явно заданный immutable URL/SHA соблюдается точно. Без HTTP origin (например, после bundle-установки) продуктовый default - `https://raw.githubusercontent.com/dewil/claude-toolkit/main`; форки заменяют default. Локальный клон не обнаруживается и не используется. Выбранный ref разрешается через GitHub API в 40-hex commit SHA; процедура, sync и bootstrap берутся из этого же SHA во временный каталог вне клиента. Уже выбранный навыком/router SHA передается процедуре точно и больше не разрешается через main; plan/apply используют тот же PINNED_URL. Immutable пара инструментов сохраняется до healthy завершения.

После конкретного плана и разрешения команды имеют вид:

```text
python3 /tmp/ai-sync/ai-sync.py plan --root ROOT --source-base PINNED_URL
python3 /tmp/ai-sync/ai-sync.py apply --root ROOT --source-base PINNED_URL --expect-plan PLAN_SHA256
python3 /tmp/ai-sync/ai-sync.py check --root ROOT
```

Plan получает полный пакет без записей в клиент, возвращает JSON с digest, changes/conflicts и applicable. Агент показывает понятные пути, версию и последствия, а не raw JSON. Apply заново вычисляет план; изменившийся digest блокирует применение. Digest связывает применение с просмотренным планом, разрешение дает пользователь. Уже согласованная совпадающая операция не требует повторного вопроса. Явно предоставленный офлайн/development bundle заменяет source-аргумент на `--bundle DIR`; сам bundle не ищется.

## Локальные изменения и контекст

Единственный реестр - `.AI/canon/canon.state.json`, intent - существующий JSON `.AI/canon/canon.intent.yaml`. Идентичность, types/adapters, проектный context-policy и неизвестные machine metadata сохраняются. По истинной per-file базе чистые файлы обновляются, локальные изменения сохраняются; одновременные локальные и upstream изменения блокируют весь apply. Removed-upstream файлы сохраняются; локальное удаление tracked source требует решения владельца, автоматического восстановления нет.

`local_only`, `skip_sync` и `overrides` задаются точными canonical paths без glob. Коллизии local-only с upstream и неизвестные существующие назначения не принимаются молча. Постоянный override сохраняет локальный файл и старую базу, пока владелец не снимет исключение. После согласованной правки intent или источников нужен новый plan; `--force` и semantic merge отсутствуют.

START и AGENTS.md/CLAUDE.md строятся из effective corpus, включая retained upstream и зарегистрированные local-only. START проверяется по receipt (`bootstrap_files` при первом sync, затем `sync.managed_start_sha256`); ручной edit START или generated входа блокирует запись. Контекст проекта редактируется в project.md. Память, docs/задачи, project.md, native settings, приватные local skills и миграционный архив сохраняются. Законные новые записи памяти или задач не блокируют согласованный plan. Sync не переносит конфиги, секреты, индекс и расписания.

## Прерывание

| Pending файл в `.ai-bootstrap/` | Владелец check/recover |
| --- | --- |
| `sync.json` | `ai-sync.py` |
| `migration.json` | `ai-migrate.py` |
| `journal.json` | `ai-bootstrap.py` |

Несколько журналов одновременно - отказ, без ручного выбора или очистки. Чужой recover не выполняется. `state.migration` после завершенной миграции - исторический receipt, не pending journal.

```text
python3 /tmp/ai-sync/ai-sync.py recover --root ROOT
python3 /tmp/ai-sync/ai-sync.py check --root ROOT
```

Для recovery используется та же immutable пара ai-sync.py/ai-bootstrap.py из pinned commit прерванной операции, сохраненная вне клиента. Нельзя смешивать установленный self-updated script с helper другого поколения. При утрате пары загружаются только соответствующие инструменты из того же подтвержденного SHA, без нового main/ref или source package; неизвестный SHA - остановка.

Sync recovery валидирует весь сохраненный журнал до записи, завершает зафиксированное поколение offline и сохраняет неожиданно измененные файлы отказом. Для replay с сохраненной парой новый пакет, сеть или выбор ref не нужны. Журнал вручную не удаляется. Все toolkit writers используют один exclusive lock (`LOCK_EX|LOCK_NB`); read-only check берет shared nonblocking lock (`LOCK_SH|LOCK_NB`) на существующий regular `.ai-bootstrap/lock`, без создания. Missing/unsafe lock - отказ. Повтор совпадающего sync/recover - no-op.

При отказе sandbox/access, read-only назначении или synthetic mount masks исполнитель останавливается и объясняет среду. Он не запускает rmdir/umount, не создает placeholder dirs и не ослабляет policy; смена на разрешенного исполнителя требует авторизации владельца. Поддержка mount masks не реализована в sync/миграторе.

Первый проверяемый контракт - Linux/Python 3.11+. Check подтверждает структуру и поколение; fresh-session проверки агентов, сертификация других ОС и массовая раскатка - отдельные шаги.

| Подстановка | Значение | Пример |
| --- | --- | --- |
| ROOT | Существующий корень клиента | `/workspace/client` |
| PINNED_URL | HTTPS raw base с полным SHA | `https://raw.githubusercontent.com/OWNER/REPO/COMMIT_SHA` |
| PLAN_SHA256 | Digest конкретного plan | 64 hex-символа из plan |
| DIR | Явно предоставленный снимок | `/tmp/approved-bundle` |
