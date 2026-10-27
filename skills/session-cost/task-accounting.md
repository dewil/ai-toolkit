# Учет расхода по задаче

Применяй эту процедуру для токенов своей активной задачи, когда нужны данные от нескольких ролей, моделей или платформ. Не включай старую смешанную сессию целиком без явного подтверждения, что она посвящена одной задаче. Не запрашивай остаток лимита аккаунта и не вызывай внешние usage API.

## Один владелец и журнал

Для каждой активной задачи держи отдельный `<taskID>.usage.json` в устойчивом служебном каталоге задачи вне синка по `rules/docs-maintenance.md`; точный абсолютный путь укажи в Markdown-владельце, записывай его атомарной заменой и не объединяй параллельные задачи. Checkpoint именуй `<taskID>.<role>.<session-id>.checkpoint.json`; новый этап той же сессии продолжает ее checkpoint, новый role/session получает свой. Владелец сохраняет ID задачи, цель, ограничения, старты отдельных ролей/сессий и handoff в Markdown. До делегирования передай task ID, роль, начало/checkpoint-файл и попроси receipt даже при сбое или retry. Каждая реальная retry-попытка - отдельный вызов; незамеренный расход помечается `unknown`, не нулем. Субагент возвращает receipt, но не редактирует общий ledger. Прежний владелец прекращает запись до передачи следующему.

В командах ниже `TOOLKIT_ROOT` — абсолютный корень с существующим `scripts/session-cost.py`, `TASK_ARTIFACTS` — проверенный абсолютный служебный каталог задачи. Задай обе переменные в том же shell; не используй пустые значения. `session-cost.py` читает явно указанный ledger; атомарную запись выполняет владелец, скрипт не переносит файлы.

```bash
: "${TOOLKIT_ROOT:?Укажи корень с scripts/session-cost.py}"
: "${TASK_ARTIFACTS:?Укажи внешний каталог задачи}"
```

Начни с отдельного пустого `<taskID>.usage.json` для конкретной задачи; например, файл `TASK-123.usage.json`:

```json
{"schema_version":1,"task_id":"TASK-123","records":[]}
```

Каждый record содержит `id`, `role`, `vendor`, `model`, `platform`, `access`, `status`, `source`, `tokens`; optional `reason` и `money`. Используй `unknown` для неподтвержденной модели, вендора или способа биллинга. Provider в имени адаптера сам по себе не доказывает vendor или `api`/`subscription`; фиксируй подтвержденное значение отдельно.

Для `measured` объект `tokens` имеет ровно `input`, `output`, `cache_read`, `cache_write`, `reasoning`: `input/output` - неотрицательные целые, разложения - неотрицательные целые или `null`, когда источник их не сообщает. Input уже включает cache read/write, output уже включает reasoning: итоги считают только input + output. При неизвестном usage используй точно `"status":"unknown", "tokens":null, "reason":"..."`, не объект из пяти null. Неизвестный usage все равно может иметь независимо подтвержденную сумму money.

Полный пример unknown receipt:

```json
{"id":"TASK-123:implementer:request-8","role":"implementer","vendor":"unknown","model":"unknown","platform":"codex","access":"unknown","status":"unknown","source":"receipt:request-8","tokens":null,"reason":"usage metadata unavailable"}
```

`money` необязателен; если известен, содержит `amount` (конечная десятичная строка >=0), `currency` (три прописные ASCII буквы), `kind` (`actual` или `estimate`), `source` (непустая ссылка/метод) и `as_of` (реальная ISO-дата). Сохраняй деньги независимо от token status; actual нельзя приписывать subscription.

Следующие модели, вендоры, request IDs, сумма, метод и дата - синтетический пример, не подтвержденный тариф или цена:

```json
{"id":"TASK-123:codex:request-7","role":"implementer","vendor":"example-vendor","model":"example-model","platform":"codex","access":"api","status":"measured","source":"synthetic-receipt:request-7","tokens":{"input":1200,"output":180,"cache_read":800,"cache_write":100,"reasoning":40},"money":{"amount":"0.012","currency":"USD","kind":"estimate","source":"synthetic illustration only; not a real rate","as_of":"2026-10-07"}}
```

Native receipt использует объект `source` с `session_id`, `provider`, `begin/end` (байтовые границы, `begin < end`), `source_file` и `prefix_sha256` (SHA-256 исходного файла до `end`). Стабильный record ID описывает вызов/интервал, не номер строки; повтор точного ID и данных учитывается один раз, конфликтующее содержимое того же ID - ошибка. Пересекающиеся native-интервалы одного provider/session/model запрещены независимо от ID и роли. Для ручного источника включай стабильный request ID; не добавляй второй агрегат, перекрывающий отдельные вызовы.

Полная форма native receipt:

```json
{"id":"TASK-123:codex:session-7:120-240:model-a","role":"reviewer","vendor":"unknown","model":"model-a","platform":"codex","access":"unknown","status":"measured","source":{"session_id":"session-7","provider":"codex","begin":120,"end":240,"source_file":"/local/session.jsonl","prefix_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},"tokens":{"input":100,"output":20,"cache_read":40,"cache_write":10,"reasoning":5}}
```

## Явный локальный snapshot

Snapshot читает только указанный файл, не ищет сессии и не читает текст сообщений в отчет. Сохрани checkpoint до работы:

```bash
python3 "$TOOLKIT_ROOT/scripts/session-cost.py" --snapshot --provider codex --file /explicit/path/session.jsonl > "$TASK_ARTIFACTS/TASK-123.implementer.session-7.checkpoint.json"
python3 "$TOOLKIT_ROOT/scripts/session-cost.py" --snapshot --provider codex --file /explicit/path/session.jsonl --since "$TASK_ARTIFACTS/TASK-123.implementer.session-7.checkpoint.json"
```

Для Claude укажи `--provider claude`. Проверь JSON snapshot и передай его `records` владельцу ledger. Ответ с `--since` также содержит следующий `checkpoint`; сохрани именно его для новой стадии и не применяй один интервал дважды. Checkpoint хранит идентификатор, длину и хеш префикса, агрегаты по модели, но не содержимое транскрипта. Файл должен быть полным UTF-8 JSONL; усеченный хвост, неверная идентичность, rollback счетчиков или измененный префикс требуют остановки и явного unknown/разбора.

Не включай файлы субагентов Claude автоматически: каждый receipt должен соответствовать одной явно выбранной сессии и этой задаче. Сессия, начавшаяся до учета без baseline, записывается как unknown, если ее нельзя безопасно отделить. Закрывая задачу, перенеси только Markdown-владельца в `docs/done/`; новый служебный ledger и checkpoints остаются по прежнему устойчивому адресу. Старые sidecars не перемещай автоматически. Уже закрытые задачи не пересчитывай.

## Codex CLI event receipt

До делегирования выбери источник receipt и зафиксируй его в handoff. Если уже разрешенный запуск того же provider через CLI дает JSON usage и сохраняет те же проверенные права и возможности, предпочти этот путь native orchestration без usage. Используй `codex exec --json`, если его stdout сохраняется целиком в приватный файл вне публичного репозитория и sync-папок. Команда читает только указанный файл, не запускает модель и не ищет историю сессий. Передавай `--model`, `--vendor` и `--access` только при подтверждении фактического запуска; иначе оставь `unknown`. Не меняй provider, права или изоляцию автоматически ради учета. Receipt содержит usage и метаданные, но не содержимое событий.

```bash
codex exec --json "<prompt>" > /private/path/TASK-123.reviewer.run-7.jsonl
python3 "$TOOLKIT_ROOT/scripts/session-cost.py" --codex-events \
  --file /private/path/TASK-123.reviewer.run-7.jsonl \
  --run-id run-7 --role reviewer \
  > /private/path/TASK-123.reviewer.run-7.receipt.json
```

Если подтверждены фактические параметры, добавь `--model`, `--vendor` и `--access` со значениями из этого запуска; неподтвержденные поля не угадывай.

Если запуск не дает пригодного JSONL с usage, зафиксируй вызов как `unknown`. Нативная orchestration-сессия без usage API также unsupported/unknown; не меняй среду и не переключай provider ради учета. Каждый retry и resume получает новый `run-id`. Повторная обработка того же receipt сохраняет те же ID, поэтому точный дубль ledger не удваивает итог; другой run ID является отдельным вызовом. Передай записи владельцу задачи, который один атомарно обновляет ledger.

Для receipt измеряются только завершенные `turn.completed.usage`: input/output обязательны; сообщенные cache-read, cache-write и reasoning сохраняются отдельно. Каждая JSONL-запись обязана иметь непустой строковый `type`; payload неиспользуемых `item.*` остается данными и игнорируется. `error` вне активного turn создает отдельную запись `unknown`; `error` внутри turn с валидным usage сохраняет измеренный turn и добавляет unknown для ошибки. Если ошибка пришла внутри turn, который затем завершился `turn.failed` или без usage, один unknown отражает и ошибку, и пропуск usage без удвоения. Ошибка после закрытия turn считается отдельным вызовом. Текст ошибки не читается и не попадает в receipt; используются фиксированные причины. Пропуск usage, незавершенный/упавший turn или error дает код 3; некорректная структура события останавливает обработку без частичного stdout (код 2). Исходный JSONL и receipt остаются приватными локальными артефактами, в ledger не копируй prompts, ответы или содержимое item.

## Сводка и ограничения

```bash
python3 "$TOOLKIT_ROOT/scripts/session-cost.py" --task-ledger "$TASK_ARTIFACTS/TASK-123.usage.json"
python3 "$TOOLKIT_ROOT/scripts/session-cost.py" --task-ledger "$TASK_ARTIFACTS/TASK-123.usage.json" --json
```

Код 0 означает complete, код 3 - partial с известными измеренными цифрами, код 2 - некорректные данные и без отчета в stdout. В отчете `total` равен `null`, если у группы есть unknown; `measured_total` и `measured_tokens` показывают только известную часть. Неизвестные optional поля остаются `null`, а известные subtotal сохраняются. Markdown показывает измеренные строки и список пропусков; unknown-only группы остаются в JSON и missing-call list, не в таблице с нулем. Подтвержденные суммы денег показываются отдельно от токенов, включая записи без измеренного usage. `cache_read` не прибавляй к input повторно, reasoning не прибавляй к output повторно. Не складывай разные валюты или actual/estimate. Не называй измеренную сумму полным итогом при unknown. Не заноси промпты, ответы, персональные данные, секреты или текст транскрипта.

Перед финальным ответом установи cutoff, добавь receipt своей финальной активности, если metadata доступна; дальнейший ответ и недоступные замеры обозначь unknown. Итоговая сгруппированная Markdown-таблица содержит поля role/vendor/model/platform/access/tokens/money/coverage (`complete`/`partial`). Короткий FINAL содержит компактную такую таблицу или ссылку на сохраненную Markdown-таблицу, не только JSON ledger. Это внутренний учет, не счет заказчику.
