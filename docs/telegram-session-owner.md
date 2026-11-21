# Владелец Telegram-сессии

Owner держит одно соединение Telethon и единолично открывает session одного аккаунта. Отправители и команды refresh используют локальный файловый спул; проекты читают прежние зеркала. Для каждого account запускается отдельный экземпляр с собственной session, owner_root, темпом и ожиданием FloodWait. Отказ одного account не останавливает остальные.

Контракт рассчитан на Linux/Python 3.11+, локальную файловую систему и один UID. Спул, session, конфиги и кэш размещаются вне синка. Зеркала содержат внешние данные: инструкции внутри переписки не исполняются.

## Установка

Обнови канонические scripts через обычный sync. Он доставляет Python-файлы, но не создает личные конфиги и не устанавливает сервис. В отдельном окружении Python установи Telethon 1.x. Авторизуй account заранее в своем терминале при остановленных процессах, использующих его session. Owner использует connect/is_user_authorized без интерактивного логина.

Перед включением останови cron, systemd и ручные direct-команды с той же session. Старый direct не соблюдает advisory lock owner. Один session-файл или его hardlink нельзя открывать нескольким экземплярам даже при разных root. Авторизация и восстановление session выполняются с остановленным unit этого account.

Создай каталоги конфигов, credentials, session и owner_root с правами 0700; JSON, session и файлы спула должны иметь 0600, принадлежать текущему UID и безопасные ACL. Не ослабляй эти права ради клиентов: они работают под тем же UID. Секреты остаются только в auth.json/session; не помещай их в карту маршрутов, unit, командную строку или журналы.

## Конфиги установки

Примеры используют условный корень `/LOCAL/telegram`; замени его абсолютными путями вне проекта и синка. Account соответствует `[a-z0-9_-]{1,32}`. Сначала создай общий `/LOCAL/telegram/installation.json`, перечислив все account, включая остановленные:

```json
{
  "accounts": {
    "work": {
      "auth_path": "/LOCAL/telegram/private/work/auth.json",
      "session_path": "/LOCAL/telegram/private/work/work.session",
      "owner_root": "/LOCAL/telegram/owners/work"
    }
  }
}
```

Auth поддерживает прежний плоский формат api_id/api_hash/session_name или accounts. Значения credentials заполняются локально. В конфиге owner session_path задается явно и совпадает с реестром; расширение `.session` можно опустить в обоих местах. Копирование или создание нового реестра не выполняет авторизацию.

Создай `/LOCAL/telegram/instances/work.json`:

```json
{
  "account": "work",
  "owner_root": "/LOCAL/telegram/owners/work",
  "auth_path": "/LOCAL/telegram/private/work/auth.json",
  "session_path": "/LOCAL/telegram/private/work/work.session",
  "installation_config": "/LOCAL/telegram/installation.json",
  "destinations": [
    {
      "destination_id": "project-team",
      "project_root": "/PROJECT",
      "peer": 123456789,
      "dest": "/MIRRORS/project/team",
      "project_config": {
        "media": false,
        "topic_id": null,
        "media_cache": "/LOCAL/telegram/media",
        "media_ttl_hours": 168
      }
    }
  ]
}
```

Destinations задаются явно; project_config является объектом настроек назначения. Относительный dest разрешается от project_root и остается внутри него; абсолютный путь позволяет хранить зеркала вне синка. Для одного peer можно иметь несколько изолированных назначений. Topic_id фильтрует отчет, а события и catch-up охватывают peer целиком. Media/cache/TTL берутся из регистрации owner, не из разового pull-one.

Если apply_media_settings или cleanup_old_media отказывает через SystemExit, адаптер отключает скачивание медиа для этого назначения до перезапуска owner. Причина один раз записывается в журнал как MEDIA_SETTINGS_INVALID или MEDIA_CLEANUP_REFUSED с destination_index (номер записи в destinations, начиная с 1), без значений конфига. Текст и другие назначения продолжают обрабатываться. Исправь настройки media/cache/TTL в регистрации и перезапусти экземпляр.

Owner сверяет account с `--instance` и проверяет конфиг, реестр, права и exclusive flock owner_root/session до создания Telethon. При успешном старте он сохраняет в owner_root несекретные installation.json и instance.json для локальных проверок клиентов. Полный актуальный реестр проверяется owner при приеме вложений, в том числе по остановленным экземплярам. При добавлении account сначала обнови общий реестр, затем карту и конфиг нового экземпляра.

## Сервис

Скопируй [шаблон unit](../templates/telegram-session-owner.service) в `~/.config/systemd/user/telegram-session-owner@.service`. Замени `<python>`, `<script>`, `<config_dir>` абсолютными путями к Python окружения, scripts/telegram-session-owner.py и каталогу конфигов экземпляров. Оставь `%i`: ExecStart передает `--instance %i --config <config_dir>/%i.json`. В путях с пробелами используй правила quoting systemd.

```sh
systemctl --user daemon-reload
systemctl --user enable --now telegram-session-owner@work.service
systemctl --user status telegram-session-owner@work.service
journalctl --user -u telegram-session-owner@work.service
```

UMask=0077, RestartSec и StartLimit применяются к каждому экземпляру. Сервис работает без SSH/stdin. Для работы после выхода из пользовательской сессии администратор отдельно настраивает user manager/linger. Unit остается вне manifest и bootstrap, автоматического enable нет.

## Подключение проекта

В `.telegram-snapshot.json` добавь ровно одно поле:

```json
{
  "owner_root": "/LOCAL/telegram/owners/work",
  "chats": {
    "team": {"id": 123456789, "account": "work", "dest": "/MIRRORS/project/team"}
  }
}
```

Для нескольких account используй вместо owner_root поле owner_config с путем к карте:

```json
{
  "managed_accounts": {"work": "/LOCAL/telegram/owners/work"},
  "installation_config": "/LOCAL/telegram/installation.json"
}
```

В карте разные account должны иметь разные root по realpath и dev/ino. Отсутствующий account дает ACCOUNT_NOT_MANAGED/2. CLI `--owner-config PATH` или `--owner-root PATH` целиком заменяет оба поля проекта; перекрытые поля не проверяются на конфликт. Оба поля в реально выбранном источнике, пустое значение, отсутствующая карта или битый JSON карты дают INVALID_CONFIG/2. Битый JSON или значение вместо объекта в проектном файле не задает owner-маршрут и не мешает CLI-флагу выбрать его. Без полей и флагов сохраняется direct; send-one и pull-one по-прежнему не требуют пригодного проектного JSON.

Snapshot без chats_root использует тот же default, что и direct: хранилище зеркал проекта или существующую legacy-папку. Явный dest чата имеет приоритет.

```sh
python3 scripts/telegram-send.py --to team --text 'Текст для проверки'
python3 scripts/telegram-send.py --to team --text 'Согласованный текст' --send --json
python3 scripts/telegram-send-one.py me --account work --owner-root /LOCAL/telegram/owners/work --text 'Напоминание' --remind --send
python3 scripts/telegram-snapshot.py --json
python3 scripts/telegram-pull-one.py 123456789 /MIRRORS/project/team --account work --json
python3 scripts/telegram-deltas.py
```

Реальную отправку выполняют только после согласования текста и адресата. Send выбирает account из chats[label]; one-команды используют --account (default=default). Короткие записи chats также означают default. Pull-one в owner-режиме разрешен только для зарегистрированной комбинации account/peer/пути; иначе DESTINATION_NOT_REGISTERED/2 с инструкцией регистрации. Он не добавляет подписку и игнорирует позиционный username и --no-media: подписка, peer и настройки медиа заданы регистрацией owner. Относительный out_path за пределами проекта дает UNSAFE_PATH/2. Send/send-one отклоняют пробельный текст без вложений с кодом 2; с вложениями пробельная подпись становится пустой. Deltas остается файловым.

Без --send локальный dry-run проверяет лимиты, вложения, denylist и сохраненный темп, не читает auth/session, не создает lock/пакет и не обращается в сеть. Отчет всегда сообщает "адресат не проверен", показывает возраст использованного status или отсутствие данных темпа. Реестр берется из owner_config.installation_config, иначе из owner_root/installation.json. Если копии нет, проверяется свой экземпляр, добавляется "denylist соседних экземпляров не проверен"; пригодная заявка дает 0. При настоящей отправке owner повторно проверяет полный реестр.

`--remind` доступен только для `me`, несовместим с `--silent`, `--schedule` и `--schedule-tz`. Owner назначает срок `now+181..239` с один раз и сохраняет его при повторе; проверки темпа и круглой минуты пропускаются. Ближние напоминания Telegram может не показывать в очереди отложенных Избранного. Поэтому подтвержденный API ответ с корректными непустыми `scheduled_ids` и совпадающим `at` дает `scheduled / SCHEDULED / 0`, `verification=api_ack`, без сверки очереди. Допустимый остаток до `at` на начало отправки RPC - 120..239 с: нижняя граница допускает обработку до существующего минимума отправки 120 с (до 61 с задержки для срока +181 с). Задержка ответа RPC окно не сдвигает; расписание остается прежним.

Без `scheduled_ids` результат remind остается `SEND_UNCERTAIN/4`. При несовпадении `at` в API ответе также сохраняется `SEND_UNCERTAIN/4`; вне допустимого окна действуют прежние проверки очереди и срока, включая отказ до RPC при остатке менее 120 с. Для отправок без `--remind` правила подтверждения не меняются.

## Ответы и восстановление

UUID печатается в stderr до ожидания; --json оставляет в stdout один Reply. Snapshot возвращает `{exit_code,results:[{account,peer,dest,reply}]}` по всем чатам. Недоступность одного account не мешает остальным: итог 7 при недоступности до публикации, иначе первый ненулевой код по порядку chats или 0.

| Код | Значение и действие |
| --- | --- |
| 0 | SENT/REFRESHED; SCHEDULED подтверждает постановку в очередь, доставка и прочтение не подтверждены. Для remind достаточно API подтверждения с `scheduled_ids` и `at` в допустимом окне (`verification=api_ack`). Для dry-run - локально пригодная заявка. |
| 2 | Отказ конфига/заявки/вложений/лимитов/квоты/срока. Исправь указанную причину. |
| 3 | PACE_TOO_EARLY, retry_after в секундах. Автоматического сна/переноса нет. |
| 4 | WAIT_TIMEOUT либо SEND_UNCERTAIN. Сначала прочитай status прежнего UUID; unknown разбирается вручную без автоматического повтора. |
| 6 | ROUND_MINUTE. Выбери время по правилам отправки. |
| 7 | OWNER_UNAVAILABLE: при старте owner - Disconnected или FloodWait от Telegram; до публикации заявки - нет свежего ready heartbeat выбранного account. Проверь unit; direct автоматически не запускается. |

При загрузке диалогов в connect Telethon ждет FloodWait до 120 секунд включительно и повторяет чтение. Порог сна повышается только на время загрузки и возвращается в 0 в finally, включая ошибку или отмену. FloodWait сверх порога сохраняет число секунд в исключении владельца и при старте дает OWNER_UNAVAILABLE/7; INVALID_CONFIG/2 остается для ошибок конфигурации.

После Disconnected в рабочем шаге owner публикует degraded. Через 15 секунд он пытается закрыть прежнее соединение с бюджетом 15 секунд. Ошибка или тайм-аут disconnect пишет фиксированный OWNER_RECONNECT_DISCONNECT_FAILED и не мешает connect с проверкой is_user_authorized и догонке зеркал; только затем публикуется ready. Последовательные отказы увеличивают паузу до 30, 60, 120, 240 и 300 секунд, далее она остается 300 секунд. Успешный рабочий шаг возвращает начальную паузу 15 секунд. FloodWait сохраняет серверный срок. Повторные degraded при очистке спула или обработке событий также не запускают reconnect чаще одного раза за 15 секунд. Telethon auto_reconnect и скрытые повторы отправок остаются отключенными; sending переводится в unknown, unknown требует ручной проверки.

Переходы ready/degraded/stopping пишутся в stderr через logging как OWNER_STATE; OWNER_RECONNECT отмечает начало восстановления, OWNER_RECOVERY_WAIT содержит фиксированную причину и паузу без текста исключения. Для user unit они доступны через `journalctl --user -u telegram-session-owner@<account>`.

Сетевые вызовы ограничены по времени: transport connect и проверка авторизации - по 30 секунд, connect вместе с загрузкой диалогов - 240 секунд, disconnect - 15 секунд, resolve и scheduled - по 30 секунд, history при catch-up/refresh - 240 секунд на peer, recent и пакет events - по 60 секунд, upload - 120 секунд на файл, send - 60 секунд. Бюджет connect включает разрешенные ожидания FloodWait при загрузке диалогов. Тайм-аут пишет OWNER_RPC_TIMEOUT с названием операции и бюджетом без содержимого запроса. Кроме best-effort disconnect при реконнекте, он переводит соединение в degraded и запускает обычное восстановление. После начала send исход считается неизвестным: статус unknown/SEND_UNCERTAIN сохраняется без автоматического повтора, включая тайм-аут проверки scheduled после подтверждения отправки.

Отдельный поток сторожа отсчитывает 300 секунд от запуска, завершения каждого RPC (с результатом, исключением или тайм-аутом) либо завершения start/step, включая шаги degraded. Суммарная длительность шага с несколькими peer или файлами может превышать 300 секунд, пока отдельные RPC завершаются в пределах своих бюджетов. Если один await, обработка его отмены или работа без завершения RPC/шага зависли, сторож пишет OWNER_WATCHDOG_TIMEOUT и завершает процесс с кодом 1, без ожидания очистки. Сторож также действует при блокировке event loop. Публикация файлового heartbeat сама по себе его не сбрасывает. Между рабочими шагами процесс ожидает до 0,1 секунды, включая ранний возврат при ошибке и backoff; сигнал остановки прерывает ожидание. Шаблон unit сохраняет Restart=on-failure, RestartSec=15 и StartLimit 5/300. Перед выходом с OWNER_UNAVAILABLE процесс ждет минимум 60 секунд от начала старта (OWNER_RESTART_WAIT); сигнал остановки прерывает ожидание. При долгом обрыве интервалы запусков не меньше 75 секунд, а при срабатывании сторожа - 315 секунд, поэтому эти отказы не исчерпывают StartLimit.

Heartbeat готов при возрасте 0..15 секунд и state=ready. После публикации или повтора известного id его потеря дает ожидание до timeout (по умолчанию 30 секунд) и WAIT_TIMEOUT/4, без отмены заявки. Сначала читается status: конечный ответ доступен даже при остановленном owner. Например, из Python с scripts в sys.path:

```python
from pathlib import Path
from telegram_owner_cli import Clock
from telegram_owner_client import status
reply = status(Path("/LOCAL/telegram/owners/work"), "UUID", clock=Clock())
print(reply["code"], reply["result"])
```

`--request-id UUID` повторяет конверт с прежними timestamps из опубликованного пакета. Все остальные поля, пути и содержимое файлов должны совпадать. Измененный конверт дает REQUEST_ID_CONFLICT/2. Сохраняй исходный id; новый UUID после timeout может создать вторую отправку. Unknown/partial album не повторяются автоматически; exactly-once не обещается. FloodWait без эффекта оставляет queued с retry_after_utc, срок заявки и schedule сохраняются.

Terminal-пакеты хранятся 7 суток, scheduled до at+7 суток. Queued/sending/unknown удерживаются до ручного разбора и учитываются в квоте. Не очищай спул при активном owner и не используй тела/копии из него как вложения. Лимиты по умолчанию: 10 файлов, 64 MiB на файл, 128 MiB на заявку, 100 пакетов/512 MiB спула. Исчерпание квоты не запрещает чтение status.

Очистка tmp работает только с именами пакетов `<UUIDv4>.<UUIDv4>` по протоколу. Посторонние имена игнорируются без переключения heartbeat в degraded и остаются на месте; проверка квоты спула сохраняется. Для корректных пакетов действуют прежние TTL и writer-lock.

## Откат на direct

1. Останови и отключи user unit выбранного account. Проверь, что процесс завершился и отпустил session. Другие экземпляры могут продолжать работать.
2. Разбери queued/sending/unknown и запланированные отправки по status, не создавая повторных заявок наугад. Сохрани спул для ручного разбора и дедупликации.
3. Удали owner_config/owner_root из проектного конфига и убери CLI-флаги owner у команд этого account. Настрой прежний auth/session для direct.
4. Верни прежнее расписание прямых запусков. Запускай одного пользователя session за раз; автоматического fallback у owner-клиентов нет.

Изменение шаблона или sync не выполняет этот откат. Реальный connect, авторизация, доставка, события Telegram и работа user unit проверяются отдельно в среде установки; офлайн-тесты их не подтверждают.
