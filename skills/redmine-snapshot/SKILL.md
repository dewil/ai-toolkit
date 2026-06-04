---
name: redmine-snapshot
description: Настроить или починить локальное зеркало открытых задач Redmine (redmine-snapshot) на устройстве. Использовать когда пользователь говорит "настрой redmine-snapshot", "подключи redmine к проекту", "где взять api-ключ redmine", "redmine-snapshot падает на SSL / сертификате", "как узнать user_id исполнителя в redmine", или когда первый запуск scripts/redmine-snapshot.py упал на авторизации/конфиге.
---

# redmine-snapshot

Скилл для первой настройки или починки локального snapshot открытых задач из Redmine через REST API. Пара скриптов держит зеркало задач команды на диске - для расчета дельт, вставки сводки в дейлики и работы без живого доступа к web-UI.

Скрипты-эталоны лежат в каноне в `scripts/redmine-snapshot.py` и `scripts/redmine-deltas.py` (top-level папка `scripts/`, не внутри папки скилла). Bootstrap-03-management копирует их в `scripts/` в корне проекта - оттуда же, где их ожидает запуск (`python3 scripts/redmine-snapshot.py`).

Архитектура повторяет `telegram-snapshot`: общие секреты на устройстве, проектные указатели - в репозитории проекта.

## Когда применять

- Пользователь впервые настраивает скрипты `scripts/redmine-snapshot.py` / `redmine-deltas.py` на новом устройстве.
- Подключается новый проект - нужно создать `.redmine-snapshot.json` и проверить, что общая авторизация на устройстве уже есть.
- Первый запуск падает на SSL (`CERTIFICATE_VERIFY_FAILED`) - Redmine за корпоративным CA.
- Нужно узнать `project_id` или `user_id` исполнителей для проектного конфига.

## Когда НЕ применять

- Обычный регулярный pull (запуск `redmine-snapshot.py` за свежими задачами) - это просто Bash-команда из шаблона дейлика, отдельный скилл не нужен.
- Анализ самих задач - они уже в `_redmine-snapshot.json`, читай напрямую.

## Алгоритм

### Шаг 1. Проверить общую авторизацию устройства

```bash
ls ~/.config/redmine-snapshot/
```

Должен быть `auth.json` (redmine_url, api_key, опционально use_curl) с правами 600. Если файла нет - идем к шагу 2.

### Шаг 2. Получить API-ключ Redmine

API-ключ привязан к аккаунту пользователя:

1. Залогиниться в Redmine в браузере.
2. Открыть "My account" (`/my/account`).
3. Справа - блок "API access key", нажать "Show". Если ключа нет - "Reset".
4. Скопировать ключ (40 hex-символов).

Если блока "API access key" нет - REST API выключен администратором (Administration -> Settings -> API -> "Enable REST web service"). Без него скрипт работать не будет, нужен админ инстанса.

### Шаг 3. Заполнить auth.json

```bash
mkdir -p ~/.config/redmine-snapshot
chmod 700 ~/.config/redmine-snapshot
```

Содержимое `~/.config/redmine-snapshot/auth.json` (выставить права 600):
```json
{
  "redmine_url": "https://redmine.example.com",
  "api_key": "<40-hex-ключ из шага 2>",
  "use_curl": false
}
```

- `redmine_url` - базовый URL инстанса без хвостового слеша.
- `use_curl` - оставить `false`. Поднять в `true` только если первый pull упал на SSL (см. шаг 5).

```bash
chmod 600 ~/.config/redmine-snapshot/auth.json
```

### Шаг 4. Подключить проект (.redmine-snapshot.json)

В корне проекта создать `.redmine-snapshot.json`:
```json
{
  "tasks_root": "tasks",
  "project_id": 123,
  "users": {
    "2551": "Иванов Иван",
    "2982": "Петров Петр"
  }
}
```

- `tasks_root` - папка под снапшот относительно корня проекта (дефолт `tasks/`). Файлы `_redmine-snapshot.json` и `_redmine-snapshot.prev.json` лягут туда.
- `project_id` - числовой id проекта. Узнать: открыть проект в web-UI, GET `<redmine_url>/projects.json` (с заголовком `X-Redmine-API-Key`), найти проект по `identifier`, взять его `id`. Идентификатор-строку из URL тоже принимает Redmine, но числовой `id` надежнее.
- `users` - карта `{user_id: "Отображаемое имя"}`. Ключи - **строки** (JSON), значения - как удобно читать в дельтах. `user_id` исполнителя: открыть его профиль в Redmine, id виден в URL (`/users/2551`); либо GET `<redmine_url>/users.json` (нужны права видеть пользователей). Имя в значении - произвольная подпись, в API не уходит, только для вывода.

Этот файл - **не секрет** (ключа в нем нет), можно коммитить в репозиторий проекта.

### Шаг 5. Первый запуск и типичные ошибки

```bash
cd <project-root>
python3 scripts/redmine-snapshot.py
```

Зависимостей нет - чистый stdlib (`urllib`). На выходе - `<tasks_root>/_redmine-snapshot.json` и построчный отчет по исполнителям.

**`ssl.SSLCertVerificationError: CERTIFICATE_VERIFY_FAILED`.** Redmine за корпоративным CA, которого нет в хранилище Python. На macOS системный/корпоративный CA обычно лежит в keychain, откуда его берет `curl`, но не `urllib`. Лечение - в `~/.config/redmine-snapshot/auth.json` выставить `"use_curl": true`: скрипт пойдет через curl-subprocess, который этот CA подхватывает. Перезапустить.

**`401 Unauthorized` / пустой ответ.** Неверный или отозванный api_key - перевыпустить в "My account" (шаг 2). Проверить, что ключ скопирован целиком (40 символов).

**`403 Forbidden` на конкретном исполнителе.** У аккаунта-владельца ключа нет прав видеть задачи этого пользователя в проекте. Скрипт пропускает такого исполнителя (`!! <имя>` в stderr) и продолжает с остальными.

**`0 задач` у всех.** Чаще всего неверный `project_id` или фильтр `status_id=open` не совпадает с настройкой статусов инстанса. Проверь, что в проекте действительно есть открытые задачи на этих исполнителей, и что `project_id` числовой и верный.

### Шаг 6. Дельты

После второго и последующих запусков рядом появляется `_redmine-snapshot.prev.json` (архив предыдущего сбора). Блок изменений для дейлика:

```bash
python3 scripts/redmine-deltas.py
```

Выведет markdown "Дельты со вчера": закрытые / новые задачи, смена статуса, смена исполнителя. Ссылки на issue строятся по `redmine_url`, записанному внутрь снапшота, - секретный `auth.json` этому скрипту не нужен.

### Шаг 7. Апгрейд скриптов в существующем проекте

Скрипты эволюционируют (новые поля, фиксы). Для апгрейда - `migrations/sync-from-canon.prompt.md`:

1. Открой Claude Code в корне проекта.
2. Скажи "сделай синк с canon" (или "sync canon").
3. Sync найдет, что `scripts/redmine-snapshot.py` / `redmine-deltas.py` разошлись с каноном, по каждому спросит "обновить?".
4. Согласишься - локальная копия перезаписывается каноном, executable-бит сохраняется. Проектные правки скрипта (если были) затрутся.

Не копировать скрипты вручную из других проектов - версии могут расходиться.

## Жесткие правила

1. **auth.json - НЕ кладем в проектную папку.** Если проектная папка синхронизируется через облако (Yandex.Disk, Dropbox, iCloud), утечка api_key в облако недопустима. Только в `~/.config/redmine-snapshot/` (локально на каждом устройстве отдельно).
2. **api_key - секрет.** В `.redmine-snapshot.json`, который коммитится, ключа быть не должно - только `project_id`, `users`, `tasks_root`.
3. **use_curl - крайняя мера.** Дефолт - `urllib`. Поднимать `use_curl` только при реальной SSL-ошибке корпоративного CA, не "на всякий случай".

## Связанные файлы

- `scripts/redmine-snapshot.py` в корне проекта - сбор открытых задач команды в `_redmine-snapshot.json` (urllib по умолчанию, curl-фолбэк для корпоративного CA, пейджинг по 100).
- `scripts/redmine-deltas.py` в корне проекта - расчет дельт между текущим и предыдущим snapshot для блока в дейликах.
- `~/.config/redmine-snapshot/auth.json` - локальные credentials на устройство (redmine_url, api_key, use_curl). Не в проекте.
- `.redmine-snapshot.json` в корне проекта - проектные указатели `{tasks_root, project_id, users}`. Не секрет.
