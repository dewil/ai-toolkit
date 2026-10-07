# Учет расхода по задаче

Применяй эту процедуру для токенов своей активной задачи, когда нужны данные от нескольких ролей, моделей или платформ. Не включай старую смешанную сессию целиком без явного подтверждения, что она посвящена одной задаче. Не запрашивай остаток лимита аккаунта и не вызывай внешние usage API.

## Один владелец и журнал

Для каждой активной задачи держи отдельный `<taskID>.usage.json` рядом с Markdown-владельцем, записывай его атомарной заменой и не объединяй параллельные задачи. Checkpoint именуй `<taskID>.<role>.<session-id>.checkpoint.json`; новый этап той же сессии продолжает ее checkpoint, новый role/session получает свой. Владелец сохраняет ID задачи, цель, ограничения, старты отдельных ролей/сессий и handoff в Markdown. До делегирования передай task ID, роль, начало/checkpoint-файл и попроси receipt даже при сбое или retry. Каждая реальная retry-попытка - отдельный вызов; незамеренный расход помечается `unknown`, не нулем. Субагент возвращает receipt, но не редактирует общий ledger. Прежний владелец прекращает запись до передачи следующему.

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
python3 scripts/session-cost.py --snapshot --provider codex --file /explicit/path/session.jsonl > TASK-123.implementer.session-7.checkpoint.json
python3 scripts/session-cost.py --snapshot --provider codex --file /explicit/path/session.jsonl --since TASK-123.implementer.session-7.checkpoint.json
```

Для Claude укажи `--provider claude`. Проверь JSON snapshot и передай его `records` владельцу ledger. Ответ с `--since` также содержит следующий `checkpoint`; сохрани именно его для новой стадии и не применяй один интервал дважды. Checkpoint хранит идентификатор, длину и хеш префикса, агрегаты по модели, но не содержимое транскрипта. Файл должен быть полным UTF-8 JSONL; усеченный хвост, неверная идентичность, rollback счетчиков или измененный префикс требуют остановки и явного unknown/разбора.

Не включай файлы субагентов Claude автоматически: каждый receipt должен соответствовать одной явно выбранной сессии и этой задаче. Сессия, начавшаяся до учета без baseline, записывается как unknown, если ее нельзя безопасно отделить. Закрывая задачу, перенеси usage-ledger и ее task/role/session checkpoints вместе с Markdown-владельцем в `docs/done/`. Уже закрытые задачи автоматически не пересчитывай.

## Сводка и ограничения

```bash
python3 scripts/session-cost.py --task-ledger TASK-123.usage.json
python3 scripts/session-cost.py --task-ledger TASK-123.usage.json --json
```

Код 0 означает complete, код 3 - partial с известными измеренными цифрами, код 2 - некорректные данные и без отчета в stdout. `cache_read` не прибавляй к input повторно, reasoning не прибавляй к output повторно. Не складывай разные валюты или actual/estimate. Не называй измеренную сумму полным итогом при unknown. Не заноси промпты, ответы, персональные данные, секреты или текст транскрипта.

Перед финальным ответом установи cutoff, добавь receipt своей финальной активности, если metadata доступна; дальнейший ответ и недоступные замеры обозначь unknown. Итоговая сгруппированная Markdown-таблица содержит поля role/vendor/model/platform/access/tokens/money/coverage (`complete`/`partial`). Короткий FINAL содержит компактную такую таблицу или ссылку на сохраненную Markdown-таблицу, не только JSON ledger. Это внутренний учет, не счет заказчику.
