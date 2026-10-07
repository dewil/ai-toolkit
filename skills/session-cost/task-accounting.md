# Учет расхода по задаче

Применяй эту процедуру для токенов своей активной задачи, когда нужны данные от нескольких ролей, моделей или платформ. Не включай старую смешанную сессию целиком без явного подтверждения, что она посвящена одной задаче. Не запрашивай остаток лимита аккаунта и не вызывай внешние usage API.

## Один владелец и журнал

Владелец задачи - единственный писатель `task-ledger.json`, рядом с Markdown-владельцем задачи. Обновляет его атомарной заменой, сохраняет ID задачи, ограничения и handoff в Markdown. Субагент возвращает receipt: роль, vendor, точную модель (или `unknown`), платформу, доступ (`api`, `subscription`, `local`, `unknown`), status, источник и числовые токены; если измерение недоступно - `status: unknown`, `tokens: null`, причина. Прежний владелец прекращает запись до передачи следующему.

Начни с пустого ledger:

```json
{"schema_version":1,"task_id":"TASK-123","records":[]}
```

Каждый record содержит `id`, `role`, `vendor`, `model`, `platform`, `access`, `status`, `source`, `tokens`; optional `reason` и `money`. `tokens` имеет ровно `input`, `output`, `cache_read`, `cache_write`, `reasoning`: неотрицательные целые или `null` для неизвестного разложения. У неизвестного расхода все `tokens` равны null. Input уже включает cache read/write, output уже включает reasoning: итоги считают только input + output. Деньги - необязательная оценка/факт в отдельной валюте и с датой; фактическую сумму нельзя приписывать subscription.

## Явный локальный snapshot

Snapshot читает только указанный файл, не ищет сессии и не читает текст сообщений в отчет. Сохрани checkpoint до работы:

```bash
python3 scripts/session-cost.py --snapshot --provider codex --file /explicit/path/session.jsonl > task-checkpoint.json
python3 scripts/session-cost.py --snapshot --provider codex --file /explicit/path/session.jsonl --since task-checkpoint.json
```

Для Claude укажи `--provider claude`. Проверь JSON snapshot и передай его `records` владельцу ledger. Ответ с `--since` также содержит следующий `checkpoint`; сохрани именно его для новой стадии и не применяй один интервал дважды. Checkpoint хранит идентификатор, длину и хеш префикса, агрегаты по модели, но не содержимое транскрипта. Файл должен быть полным UTF-8 JSONL; усеченный хвост, неверная идентичность, rollback счетчиков или измененный префикс требуют остановки и явного unknown/разбора.

Не включай файлы субагентов Claude автоматически: каждый receipt должен соответствовать одной явно выбранной сессии и этой задаче. Сессия, начавшаяся до учета без baseline, записывается как unknown, если ее нельзя безопасно отделить.

## Сводка и ограничения

```bash
python3 scripts/session-cost.py --task-ledger task-ledger.json
python3 scripts/session-cost.py --task-ledger task-ledger.json --json
```

Код 0 означает complete, код 3 - partial с известными измеренными цифрами, код 2 - некорректные данные и без отчета в stdout. `cache_read` не прибавляй к input повторно, reasoning не прибавляй к output повторно. Не складывай разные валюты или actual/estimate. Не называй измеренную сумму полным итогом при unknown. Не заноси промпты, ответы, персональные данные, секреты или текст транскрипта.

Перед финальным ответом установи cutoff, добавь receipt своей финальной активности, если metadata доступна; дальнейший ответ и недоступные замеры обозначь unknown. Короткий результат: измеренные input/output и cache-разложение, количество unknown, coverage, известная стоимость с валютой/типом или unknown, ссылка на локальный ledger. Это внутренний учет, не счет заказчику.
