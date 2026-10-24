# Запуск Codex для review и implement

Справочник адаптера, не отдельный навык. Сначала [маршрутизация моделей](../../rules/model-routing.md): прочитай свежий MODELS и запиши выбранные модель, effort, сервер и ревизию. Профили не заменяют текущие назначения. Все ограничения доступа, квот и resume ниже обязательны.

## Файловая песочница

Все запуски, включая `resume`, идут через `scripts/codex-sandbox.py`: bwrap оставляет видимыми объект, CODEX_HOME и системные файлы, перекрывает секреты по именам. В аудите объект доступен только на чтение; HOME и /tmp пустые, сеть общая. При аудите основного чекаута видны секреты в истории git и `.git/config` (в том числе токены в URL remote); утекшие секреты требуют ротации. Перекрытие идет только по именам: `credentials.json`, `.aws/`, базы и содержимое `venv`/`node_modules` остаются видимыми. `<toolkit>` в шаблонах - абсолютный путь к клону AI Toolkit; `-C` подставляет скрипт. Код 2 - сообщить пользователю причину из stderr, при отсутствии bwrap поставить `bubblewrap`; без рабочей песочницы Codex не запускать. Скрипт поддерживает только Linux; на macOS он отказывает.

## Жесткие правила

1. **Всегда** запускать с `-s read-only`. Других режимов в этом скилле нет.
2. **Всегда** фиксировать рабочий каталог через `-C <project-root>`. В `-C` подставляй корень текущего проекта (абсолютный путь рабочего каталога), не хардкодь путь.
3. **Всегда** явно фиксировать модель и effort в команде:
   - Модель и effort выбираются по свежему MODELS либо каноническому дефолту [model-routing](../../rules/model-routing.md), не по TUI или старой строке model профиля. Профили `audit`, `compliance`, `implement` сохраняются как обязательный транспорт и настройки доступа. Выбор передается явно в `-c model` и `-c model_reasoning_effort`.
   - `minimal` запрещен: старый адаптер на нем ломается. До запуска задай `AI_REVIEW_MODE` выбранным режимом (включая compliance при PROFILE=audit для не-Codex автора), `AI_EFFECT_IRREVERSIBLE=1` для необратимого эффекта, `AI_SELECTED_MODEL`, `AI_SELECTED_EFFORT` из выбранного назначения. Для пары реализации/сверки задай `AI_PAIR_MODEL`: выбранная модель будущего проверяющего при реализации, подтвержденная actual-модель автора при compliance. Не угадывай ее по имени профиля. Неизвестная пара блокирует запуск; после ответа actual сверяется повторно.
   - Compliance не low; для необратимого эффекта требуется high. Другой effort допускается только если совместим с гейтом, выбран явно и поддержан адаптером. Изменившееся назначение при resume требует новой сессии с handoff, не молчаливого наследования.

4. **План** класть в `docs/<plan-file>.md` (рабочая заметка, не едет на прод; имя файла - по таблице "Режимы").
5. **Отчет** писать в `docs/<report-file>.md` через shell-редирект, потом читать через `Read`.
6. Bash-таймаут поднимать до 600000ms (10 мин) - codex может думать долго, особенно на `high`.
7. **Всегда** после сессии собирать статистику лимитов аккаунта (см. блок "Статистика лимитов после сессии") - дописывать в конец `<report-file>.md` и показывать пользователю в саммари. Без этого скилл считается выполненным некорректно: непонятно, насколько мы близко к ограничению по тарифу. Статистика по самим токенам (input/output/reasoning) намеренно не собирается - она шум, важны только проценты лимитов.
8. **Всегда** перед запуском codex делать pre-check окна `primary` лимита по последнему rollout (на тарифе с двумя окнами это короткое `5h`, на тарифе с одним - недельное; метка берется из `window_minutes`) (см. блок "Pre-check лимита перед запуском"). Если осталось `< 10%` - НЕ запускать codex; вернуть пользователю алерт с фактическим остатком и временем до сброса, и попросить решить (подождать или все равно запустить). Иначе мы тратим контекст и время на запуск, который завершится частичным ответом или ошибкой лимита. Пустой результат читается **двумя разными состояниями, и вести себя надо по-разному**:
   - **папка codex найдена, rollout-логов в ней нет** - это действительно первый запуск на машине: предупредить пользователя и разрешить запуск;
   - **домашняя папка codex не резолвилась вовсе** - это не первый запуск, а промах резолва: проверка лимита **не выполнялась**, и про остаток квоты мы не знаем ничего. Не рапортовать это тоном штатной ветки - сказать пользователю, что путь не определился и лимит не проверен, и спросить, запускать ли. Резолв - в блоке "Резолв домашней папки codex".
9. **Домашнюю папку codex резолвить, а не брать из окружения** (блок "Резолв домашней папки codex"). Не заменяй сниппеты на `~/.codex` и не пиши `~/.codex` в собственных командах. Одного `"${CODEX_HOME:-$HOME/.codex}"` мало: он защищает от захардкоженного пути, но не от того, что переменная задана в интерактивном профиле пользователя и в неинтерактивный shell агента не доходит - тогда фолбэк берет пустую `$HOME/.codex`, и промах выглядит штатно. Признак - pre-check рапортует "первый запуск на машине" на устройстве, где codex заведомо работал (наблюдалось: `CODEX_HOME=/data/.codex` в профиле, 141 rollout-файл на диске, pre-check отчитался о первом запуске). Промах бьет по трем местам сразу: pre-check рапортует "rollout-логов нет", пост-сбор статистики не находит сессию, сверка `session id` перед `resume` объявляет настоящий id сфабрикованным. Четвертое место - вне проверок скилла, и оно самое дорогое: команда, запущенная руками (`codex exec`, `login status`, `resume`), при том же промахе отвечает так, будто логин слетел (`401 Missing bearer`, `Not logged in`, `no rollout found for thread id` - разбор в "Ограничениях"). Пока резолв не выполнен, о потере авторизации не сообщать и `codex login` не предлагать.
10. **В не-git проекте обязателен `--skip-git-repo-check`.** Перед запуском проверить, git ли проект (`git -C <project-root> rev-parse >/dev/null 2>&1`); если нет - добавлять флаг во ВСЕ вызовы `codex exec`, включая `resume`. Без него codex не стартует вообще, причем молча (rc=0 и отчет из двух строк - см. "Ограничения"). Флаг снимает только проверку trusted-каталога, `-s read-only` продолжает действовать. Актуально для проектов типов wiki и management - это обычно vault'ы и папки заметок без git.

## Шаблон вызова

Сначала **резолв домашней папки codex, проверка профиля и выбор модели по MODELS** (правило 3). Флаг собираем в массив `MODEL_FLAG`; профиля нет - сниппет останавливается, дефолт codex не подставляется:

```bash
if ! { [ -d "$CODEX_HOME/sessions" ] && [ -r "$CODEX_HOME/sessions" ] && [ -x "$CODEX_HOME/sessions" ]; }; then
  CODEX_HOME=$(grep -hs -E '^[[:space:]]*(export[[:space:]]+)?CODEX_HOME=' \
      ~/.bashrc ~/.bash_profile ~/.profile ~/.zshrc ~/.zshenv 2>/dev/null \
    | sed -E "s/.*CODEX_HOME=//; s/^[\"']//; s/[\"']$//; s|^~|$HOME|; s|\\\$HOME|$HOME|g; s|/+$||" \
    | while IFS= read -r d; do [ -n "$d" ] && [ -d "$d/sessions" ] && [ -r "$d/sessions" ] \
        && [ -x "$d/sessions" ] && echo "$d"; done | sort -u)
  [ -z "$CODEX_HOME" ] && [ -r "$HOME/.codex/sessions" ] && [ -x "$HOME/.codex/sessions" ] \
    && CODEX_HOME="$HOME/.codex"
  [ "$(printf '%s' "$CODEX_HOME" | grep -c .)" -gt 1 ] && CODEX_HOME=""
fi
{ [ -d "$CODEX_HOME/sessions" ] && [ -r "$CODEX_HOME/sessions" ] && [ -x "$CODEX_HOME/sessions" ]; } || CODEX_HOME=""
# экспорт - для дочернего процесса (проверкам скилла он не нужен: они читают
# файлы в этом же bash). Пустое значение не экспортируем - не затирать окружение
[ -n "$CODEX_HOME" ] && export CODEX_HOME
PROFILE=audit   # роль этого скилла; сверка реализации codex (compliance после implement) ставит compliance, implement - implement
PROFILE_FILE="$CODEX_HOME/$PROFILE.config.toml"
[ -n "$CODEX_HOME" ] || { echo "PROFILE: папка codex не резолвилась - стоп, см. 'Резолв домашней папки codex'"; exit 1; }
[ -e "$PROFILE_FILE" ] || { echo "PROFILE: нет $PROFILE_FILE - создай файл строкой model = \"<slug>\" и повтори; на дефолт codex скилл не идет"; exit 1; }
read_profile_model() {
  python3 - "$1" <<'PYMODEL'
from pathlib import Path
import sys
import tomllib
try:
    with Path(sys.argv[1]).open('rb') as stream:
        model = tomllib.load(stream).get('model')
    if (not isinstance(model, str) or not model or model != model.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in model)):
        raise ValueError('Invalid model identity')
except (OSError, ValueError):
    sys.exit(1)
print(model)
PYMODEL
}
PROFILE_MODEL=$(read_profile_model "$PROFILE_FILE")
[ -n "$PROFILE_MODEL" ] || { echo "PROFILE: в $PROFILE_FILE нет читаемого model (права, ключ, TOML) - поломка конфига, стоп"; exit 1; }
# Значения подготовлены из свежего MODELS/канонического дефолта; профиль не переписываем.
[ -n "${AI_SELECTED_MODEL:-}" ] && [ -n "${AI_SELECTED_EFFORT:-}" ] || { echo "ROUTING: выбери модель/effort по model-routing.md"; exit 1; }
case "$AI_SELECTED_EFFORT" in
  low|medium|high|xhigh|max|ultra) ;;
  *) echo "ROUTING: unsupported effort (minimal запрещен)"; exit 1 ;;
esac
AI_REVIEW_MODE=${AI_REVIEW_MODE:-$PROFILE}
if [ "$AI_REVIEW_MODE" = compliance ] && [ "$AI_SELECTED_EFFORT" = low ]; then
  echo "ROUTING: compliance requires at least medium"; exit 1
fi
if [ "${AI_EFFECT_IRREVERSIBLE:-0}" = 1 ]; then
  case "$AI_SELECTED_EFFORT" in high|xhigh|max|ultra) ;; *) echo "ROUTING: irreversible effect requires high"; exit 1 ;; esac
fi
MODEL_FLAG=(-p "$PROFILE" -c model="$AI_SELECTED_MODEL")
# инвариант ролей: автор (implement) и его проверяющий (compliance) обязаны отличаться; audit может совпадать с любым
OTHER=""; [ "$PROFILE" = implement ] && OTHER=compliance; [ "$PROFILE" = compliance ] && OTHER=implement
OTHER_MODEL=""; [ -n "$OTHER" ] && OTHER_MODEL=$(read_profile_model "$CODEX_HOME/$OTHER.config.toml")
[ -n "$OTHER" ] && [ -z "$OTHER_MODEL" ] && { echo "PROFILE: нет читаемой модели в $OTHER.config.toml - независимая пара не определена, стоп"; exit 1; }
{ [ -n "$OTHER" ] || [ "$AI_REVIEW_MODE" = compliance ]; } && [ -z "${AI_PAIR_MODEL:-}" ] && { echo "ROUTING: независимая пара неизвестна"; exit 1; }
{ [ -n "$OTHER" ] || [ "$AI_REVIEW_MODE" = compliance ]; } && [ "$AI_PAIR_MODEL" = "$AI_SELECTED_MODEL" ] && { echo "ROUTING: автор и проверяющий на одной модели"; exit 1; }
```

Строки `PROFILE: ...` - для пользователя: "профиля нет" и "файл есть, но не прочитан" - два разных состояния, оба останавливают запуск; "одна выбранная модель автора и проверяющего" и "нет читаемой модели второго профиля пары" тоже останавливают запуск до вызова модели. Роль `audit` не требует пары автоматически, но при сверке проверяется отличие от фактической модели автора. Профиль читается через stdlib `tomllib` (Python 3.11+), не через `source`: TOML должен быть корректным, `model` - непустой строкой без крайних пробелов и управляющих символов. Другие ключи профиля применит сам Codex через `-p`; сравнение строк не доказывает различие CLI-алиасов, фактическая модель подтверждается по запуску. Битое значение в профиле (например, `sandbox_mode = "nonsense"`) роняет запуск громко - `Error loading config.toml`, rc=1, отчет из двух строк; неизвестные ключи codex терпит (проверено 04.09.2026). Какая модель шла на самом деле, показывает rollout, а не наши флаги: пост-сбор статистики печатает ее строкой `model:`.

Затем **проверить, git ли проект**: в не-git каталоге codex откажется стартовать (см. правило 10 и "Ограничения"). Флаг собираем в массив по той же схеме - в git-проекте он пуст и в команду не попадает:

```bash
GIT_FLAG=(); git -C <project-root> rev-parse >/dev/null 2>&1 || GIT_FLAG=(--skip-git-repo-check)
```

Каждый запуск (обычный, отвязанный, resume) - с `-c project_doc_max_bytes=0`: codex не подгружает AGENTS.md проекта. Файлы читать он по-прежнему может.

Дальше подставить массивы в вызов:

```bash
python3 "<toolkit>/scripts/codex-sandbox.py" audit --root "<project-root>" -- \
  -s read-only -c project_doc_max_bytes=0 \
  "${MODEL_FLAG[@]}" \
  "${GIT_FLAG[@]}" \
  -c model_reasoning_effort="$AI_SELECTED_EFFORT" \
  < docs/<plan-file>.md \
  > docs/<report-file>.md 2>&1
```

**План подается через stdin, а не аргументом.** У агента stdin не терминал, и в этом режиме `codex exec` ждет промт именно оттуда: при вызове с планом-аргументом он печатает `Reading additional input from stdin...` и висит, пока stdin не закроется. Прогон уходит в таймаут целиком - наблюдалось 10 минут впустую при `timeout: 600000`. Форма с аргументом рабочая только в интерактивном терминале, поэтому в скилле ее нет. Если аргумент по какой-то причине нужен, добавляй `< /dev/null`, иначе повиснет то же самое.

Легенда: `"${MODEL_FLAG[@]}"` - `-p audit -c model=<выбранный slug MODELS/default>` (сверка реализации codex - `-p compliance`, у `implement` - `-p implement`, модель передается явно поверх профиля); `"${GIT_FLAG[@]}"` - `--skip-git-repo-check` в не-git проекте (или ничего в git-проекте); `AI_SELECTED_EFFORT` - по свежему MODELS с учетом обязательного минимума из таблицы "Режимы": `medium` (audit, adversarial, advocate, second-opinion, compliance) или `high` (escalation и adversarial на необратимом); назначение берется из MODELS/default с сохранением обязательных минимумов; `<plan-file>` / `<report-file>` - имена из таблицы "Режимы"; `<project-root>` - абсолютный путь корня текущего проекта, в котором сейчас идет работа.

**Резолв (включая `export CODEX_HOME`) и `codex exec` - в одном Bash-вызове.** Shell-состояние между Bash-вызовами агента не переживает: резолв из предыдущей команды в следующей уже не виден. Без экспорта дочерний codex берет дефолт `~/.codex`, где на машине с нестандартным путем нет авторизации, - и падает в `401`, хотя все проверки скилла (pre-check, резолв модели) отработали штатно в том же bash.

Если план уже в переменной или буфере, а не в файле - тот же вызов, только stdin из here-string: `<<< "$PLAN"` вместо `< docs/<plan-file>.md`.

### Опционально: resume существующей сессии

Для SDD-compliance этот шаблон применяется только к правкам из исходного отчета при исходном `medium`. Другой effort или новый вопрос - новая проверяющая сессия с исходной спекой и отчетом; общий совет о наследовании глубокого разбора ниже этот гейт не отменяет.

Сессию, начатую без флага, не продолжай - запускай заново.

Для повторного раунда по той же теме (re-review после применения фиксов, уточнение мутного ответа, догрузка вопросов) можно переиспользовать сессию Codex через `resume <SESSION_ID>` - тогда Codex не нужно заново скармливать план, файлы и фрейминг, он помнит контекст первого прохода. Экономит входной токен-бюджет и сохраняет преемственность анализа.

```bash
python3 "<toolkit>/scripts/codex-sandbox.py" audit --root "<project-root>" -- -s read-only -c project_doc_max_bytes=0 "${GIT_FLAG[@]}" \
  resume "$SESSION_ID" \
  < docs/<followup-plan-file>.md \
  > docs/<followup-report-file>.md 2>&1
```

> **Порядок флагов.** `-C` подставляет скрипт. Флаги codex после `--` (`-s`, `-c`, `--skip-git-repo-check`) ставь до подкоманды `resume`; после нее codex принимает только аргументы продолжения.

`SESSION_ID` берется из шапки предыдущего отчета (Codex по нашей просьбе пишет `session id: <id>` первой строкой - см. "Упаковка контекста в план"):

```bash
SESSION_ID=$(awk -F': ' '/^session id:/ {print $2; exit}' docs/<previous-report-file>.md)
```

**Шапке отчета не доверяй вслепую** - модель может сфабриковать id (наблюдалось: в отчете UUID, которого нет ни в одном rollout). Перед resume сверь, что файл `$CODEX_HOME/sessions/*/*/*/rollout-*-<SESSION_ID>.jsonl` существует (`CODEX_HOME` - через резолв из блока "Резолв домашней папки codex", иначе сверка объявит настоящий id сфабрикованным); нет - настоящий id достань из имени свежайшего rollout-файла (`ls -1t "$CODEX_HOME"/sessions/*/*/*/rollout-*.jsonl | head -1`). Папка не резолвилась - сверять нечем: скажи это прямо, а не выдавай "id не найден". Учти при этом, что codex в multi-agent-режиме пишет рядом с основной сессией rollout'ы **субагентов** (таймстампы почти совпадают, и свежайшим может оказаться именно субагентский): resume субагентского id падает с `direct app-server input is not allowed for multi-agent v2 sub-agents (code -32600)`. Поймал эту ошибку - не перебирай соседние файлы той же минуты (наблюдалось, что субагентскими оказываются все); надежный кандидат - id, которым сессия запускалась изначально (из первого запуска или прошлого resume - он остается resumable через все раунды).

Особенности resume:
- `-c model="..."` и `-c model_reasoning_effort="..."` в resume **игнорируются** - resume наследует модель и effort исходной сессии. Вне SDD-compliance это допустимо для re-review того же режима; для SDD действует ограничение `medium` и правок исходного отчета выше, но именно отсюда молчаливый перерасход: продолжение глубокого аудита короткой консультацией тратит `high` на обычную развилку. Дефолт и обмен - в разделе "Как встроить валидацию в поток"; нужен другой уровень - стартуй свежую сессию, не resume. Следствие для выбора модели: первую сессию открывай той моделью, которой хочешь вести все раунды, - `resume` ее не сменит.
- Просьба про `session id` в новом плане **не нужна** - sessionID у продолженной сессии тот же; пост-сбор лимитов работает по тому же `SESSION_ID` (можно его же переиспользовать или достать из новой шапки, если Codex ее напишет).
- Pre-check лимита перед resume - такой же обязательный, как перед новым запуском: короткое окно могло быть исчерпано предыдущим раундом.
- Для каждой итерации используй отдельные имена файлов плана/отчета (`<plan-file>-r2.md`, `<report-file>-r2.md`, дальше `-r3` и т.д.), не перезаписывай предыдущие - история раундов нужна для разбора.

### Опционально: фоновый запуск для долгих аудитов

`codex exec` синхронный и на `high` может думать минутами, блокируя сессию Claude. Прежняя редакция называла `run_in_background: true` у Bash-тула надежным способом не ждать. **Для долгих прогонов это не так**: харнесс снимает фоновую задачу на середине - дважды на обкатке `implement` 04.09.2026 (`status: killed` без причины) и в тот же день в сессии канона (вывод задачи `[killed]`, в журнале ядра за три дня ни одного OOM - убила не система). Симптом тихий: отчет пуст или оборван, ошибки нет, rollout есть - наполовину (`rules/silent-failure.md`).

Поэтому: **прогон в несколько минут - foreground с `timeout: 600000`**, ждать его дешевле, чем разбирать снятый. **Прогон дольше - только отвязанным процессом по протоколу ниже.** `run_in_background` для `codex exec` не использовать.

Независимо от формы запуска **stdin обязан быть закрыт или занят планом** - иначе без TTY codex ждет "additional input from stdin" до EOF, которого не будет, и виснет с пустым отчетом (наблюдалось зависание почти на 7 часов; "процесс жив" - не признак прогресса, проверяй, что отчет растет). Форма из "Шаблона вызова" (`< <plan-file>`) это закрывает сама. Поймал зависание (отчет не растет, в нем "Reading additional input from stdin...") - убей процесс и перезапусти план через stdin; потерь нет, зависшая сессия фактически не начиналась. Pre-check лимита - до запуска, как обычно.

### Отвязанный запуск - протокол

Отвязка - не одна команда, а набор свойств, каждое из которых закрывает свой отказ. Тот же механизм в `rules/scheduled-automation.md` для вложенного `claude -p`; принцип там же: процесс, чей результат должен пережить вызов тула или сессию, нуждается во внешнем владении жизненным циклом - журнале, PID и проверяемом статусе завершения. Отвязывать только такое: прогон codex - да; миграцию, сборку или тесты с записью, которые должны отмениться вместе с сессией, - нет.

- **Свой каталог запуска на каждый прогон**: `docs/codex-runs/<topic>-<дата-время>/` - в нем план, отчет, `pid`, лог обвязки и маркер `exit`. Старый маркер не может быть принят за новый: у нового прогона свой каталог.
- **`setsid nohup`, stdin из плана, stdout в отчет** - процесс в собственной сессии, харнесс его не снимает. PID пишет **сам дочерний скрипт** (`echo $$`), а не `$!` снаружи: `setsid` из фонового задания форкается, и `$!` указывает на уже завершившуюся обертку - проверено 04.09.2026, группа по нему не снималась.
- **Маркер завершения атомарный и с кодом выхода**: пишется во временный файл и переименовывается. "Файл `exit` есть" - единственный признак конца; "процесса нет в `ps`" им не является. Отмена тоже оставляет маркер: `trap` на `TERM` пишет `cancelled` (проверено - без него убитая группа маркера не оставляет, и ожидание не кончается никогда). Ненулевой код, `cancelled` или отсутствие маркера - **не результат**: отчет не читать как готовый, к следующему шагу не переходить.
- **Команда отмены известна до запуска**: `kill -TERM -- -$(cat "$RUN/pid")` - группа процессов, а не один PID (после `setsid` они совпадают, и `pid` записан изнутри). Отвязанный процесс переживает сессию Claude: отменил задачу пользователь - процесс снимается этой командой, сам он не остановится.
- **Ожидание - инструментом ожидания условия** (`Monitor` в Claude Code) по появлению файла `exit`, не `sleep` в цикле и не перечитывание отчета.
- **Заподозрил обрыв по лимиту раньше маркера и проверяешь растущий отчет** - искать точную привязанную строку `^ERROR: You've hit`, а не слово "лимит"/"usage limit": codex может процитировать эти слова в собственном отчете о прогрессе, и грепп по слову тогда даст ложную тревогу.
- **Резолв `CODEX_HOME` и `export` - в том же Bash-вызове**, до `setsid`: дочерний процесс наследует окружение, между вызовами оно не живет.
- **Preflight до `setsid`**: каталог создан, план скопирован, `setsid` есть - любой отказ останавливает запуск, а не идет дальше с пустым отчетом. Суффикс `-$$` в имени каталога снимает коллизию двух запусков в одну секунду. **Рукопожатие после `setsid`**: `pid` ждется ограниченно (до 10 с), проверяется, что это число и что процесс - лидер своей группы; иначе запуск не считается управляемым и разбирается как сбой, а не объявляется успешным с `pid ?`.

```bash
<блок резолва CODEX_HOME / MODEL_FLAG (профиль роли) / GIT_FLAG из "Шаблона вызова", с export CODEX_HOME>
PROJECT_ROOT="<project-root>"   # абсолютный путь, в кавычках: пробел в пути иначе разорвет аргументы
RUN="docs/codex-runs/<topic>-$(date +%Y%m%d-%H%M%S)-$$"; mkdir -p "$RUN" || exit 1
cp "docs/<plan-file>.md" "$RUN/plan.md" || { echo "плана нет - стоп"; exit 1; }
command -v setsid >/dev/null || { echo "setsid нет - установи util-linux"; exit 1; }
setsid nohup bash -c '
  trap '"'"'echo cancelled > "$0/exit.tmp" && mv "$0/exit.tmp" "$0/exit"; exit 143'"'"' TERM
  echo $$ > "$0/pid"
  python3 "<toolkit>/scripts/codex-sandbox.py" audit --root "$1" -- -s read-only -c project_doc_max_bytes=0 "${@:2}" < "$0/plan.md" > "$0/report.md" 2>&1 & wait $!
  rc=$?; echo "$rc" > "$0/exit.tmp" && mv "$0/exit.tmp" "$0/exit"
' "$RUN" "$PROJECT_ROOT" "${MODEL_FLAG[@]}" "${GIT_FLAG[@]}" -c model_reasoning_effort="$AI_SELECTED_EFFORT" \
  > "$RUN/nohup.log" 2>&1 < /dev/null &
for i in 1 2 3 4 5 6 7 8 9 10; do [ -s "$RUN/pid" ] && break; sleep 1; done
PID=$(cat "$RUN/pid" 2>/dev/null); case "$PID" in ''|*[!0-9]*) echo "PID не получен за 10 с - запуск не управляем, разбирай $RUN/nohup.log"; exit 1;; esac
[ "$(ps -o pgid= -p "$PID" | tr -d ' ')" = "$PID" ] || { echo "pid $PID не лидер группы - отмена группой не сработает"; exit 1; }
echo "запущен: $RUN, pid $PID; отмена: kill -TERM -- -$PID"
```

`<topic>` в имени каталога - слаг из `[a-z0-9-]`, не фраза пользователя как есть: `/` и `..` в нем уводят каталог прогона из `docs/codex-runs/`, и это делает внешний shell, до песочницы codex.

Для `implement` та же внешняя обвязка `setsid`, внутри - команда скрипта из его шага 4; `PROFILE=implement` в резолве. Если задание порождает промежуточные файлы (черновики, побочные результаты), в плане явно называть каталог назначения (например, `_рабочие/`) - без этого codex сорит черновиками прямо в корень результата: наблюдалось до полусотни файлов за один прогон.

**После маркера - извлечь итоговый ответ.** `$RUN/report.md` - это весь stdout `codex exec`: шапка, план, трассировка вызовов инструментов и рассуждений, и только в конце ответ. На живых аудитах это 7-31 тысяч строк при ответе в 200-300 (cactus-adm, 05.09.2026; в сессии канона 04.09 - 5400 строк при 200). Читать такой файл `Read` нельзя: целиком не влезет, а по частям затянет в основной контекст трассировку. Маркеры устойчивы (проверено на codex 0.145 и 0.153): ответ начинается после **последней** строки `codex` (метка хода модели, их несколько - по числу ходов) и кончается перед **первой после нее** строкой `tokens used` (после нее тело ответа может повториться целиком - потоковая выдача плюс финальная, первый `tokens used` снимает дубль). Настоящий `session id` стоит в шапке, которую печатает сам codex, и берется **до** обрезки: в теле ответа модель его выдумывает.

```bash
L=$(grep -n '^codex$' "$RUN/report.md" | tail -1 | cut -d: -f1)
T=$(awk -v l="$L" 'NR>l && /^tokens used/ {print NR; exit}' "$RUN/report.md")
SID=$(awk -F': ' '/^session id:/ {print $2; exit}' "$RUN/report.md")   # шапка codex, не тело ответа
[ -n "$L" ] && [ -n "$T" ] || { echo "маркеров нет - отчет оборван, это не результат"; exit 1; }
{ echo "session id: $SID"; sed -n "$((L+1)),$((T-1))p" "$RUN/report.md"; } > "docs/<report-file>.md"
```

Дальше все штатно: `docs/<report-file>.md` - обычный отчет режима с `session id` первой строкой, статистика лимитов дописывается в него сниппетом "Статистика лимитов после сессии" без правок, владелец задачи читает его по процессу review. `$RUN/report.md` остается полным логом прогона для разбора. Каталог `docs/codex-runs/` должен быть в `.gitignore` проекта; если правила еще нет, добавь его перед первым отвязанным запуском. Логам по 30 тысяч строк в репозитории не место.

## Как codex выбирает модель

MODELS перечитывается перед каждым новым заданием/запуском; при отсутствии файла или строки - конкретный дефолт rules/model-routing.md. Профили `audit`, `compliance`, `implement` обязательны для настроек доступа, но не являются источником актуального назначения. Их model проверяется как валидная конфигурация, затем выбранное назначение передается явным `-c model`. Actual модель проверяется по rollout. Неподдержанный effort/модель/сервер не разрешает подмену. Не используй TUI-default.

## Резолв домашней папки codex

Все проверки скилла (pre-check лимита, пост-сбор статистики, сверка `session id`) читают `sessions/` и `config.toml` из домашней папки codex. Ее расположение переопределяется переменной `CODEX_HOME`, и **написать `"${CODEX_HOME:-$HOME/.codex}"` недостаточно**: переменная обычно задана в интерактивном профиле пользователя и в неинтерактивный shell агента не приходит. Тогда фолбэк честно берет `$HOME/.codex`, там пусто - и проверка молча превращается в "логов нет, первый запуск на машине".

Поэтому папку **резолвим**, а не берем из окружения. Сниппет вставляй в начало каждого блока, где путь нужен: shell-состояние между вызовами не переживает, экспорт из прошлой команды в следующей уже не виден. Блок, который сам запускает `codex exec`, добавляет к сниппету строку `export CODEX_HOME` (см. "Шаблон вызова"): проверкам скилла экспорт не нужен (читают файлы в том же bash), а дочернему процессу codex - обязателен, без него он уходит в дефолт `~/.codex` и падает в `401`.

```bash
if ! { [ -d "$CODEX_HOME/sessions" ] && [ -r "$CODEX_HOME/sessions" ] && [ -x "$CODEX_HOME/sessions" ]; }; then
  CODEX_HOME=$(grep -hs -E '^[[:space:]]*(export[[:space:]]+)?CODEX_HOME=' \
      ~/.bashrc ~/.bash_profile ~/.profile ~/.zshrc ~/.zshenv 2>/dev/null \
    | sed -E "s/.*CODEX_HOME=//; s/^[\"']//; s/[\"']$//; s|^~|$HOME|; s|\\\$HOME|$HOME|g; s|/+$||" \
    | while IFS= read -r d; do [ -n "$d" ] && [ -d "$d/sessions" ] && [ -r "$d/sessions" ] \
        && [ -x "$d/sessions" ] && echo "$d"; done | sort -u)
  [ -z "$CODEX_HOME" ] && [ -r "$HOME/.codex/sessions" ] && [ -x "$HOME/.codex/sessions" ] \
    && CODEX_HOME="$HOME/.codex"
  [ "$(printf '%s' "$CODEX_HOME" | grep -c .)" -gt 1 ] && CODEX_HOME=""
fi
{ [ -d "$CODEX_HOME/sessions" ] && [ -r "$CODEX_HOME/sessions" ] && [ -x "$CODEX_HOME/sessions" ]; } || CODEX_HOME=""
```

На выходе `CODEX_HOME` либо указывает на папку с `sessions/`, либо **пуст - это отдельное состояние "не нашли"**, и трактовать его как первый запуск нельзя (см. правило 8). Свойства, которые важно сохранить при любой переработке сниппета:

- **кандидат проверяется тройкой `-d` + `-r` + `-x` на `sessions/`**, и каждая часть закрывает свой промах. Без `-d` кандидатом становится обычный файл с таким именем. Без `-r` проходит папка, которую нельзя прочитать. Без `-x` проходит папка, которую можно перечислить, но нельзя обойти вглубь - а rollout лежит на три уровня ниже (`sessions/<год>/<месяц>/<день>/`), и глоб вернет пусто. Любой из этих промахов дает один и тот же результат: логов "нет", и предохранитель квоты вырождается в ложный "первый запуск". Одного `ls` для этого мало - он успешен во всех трех случаях. Пустая, но полноценная `sessions/` тройку проходит: это и есть настоящий первый запуск;
- **несколько РАЗНЫХ живых кандидатов - тоже "не нашли"**, а не повод взять первый: в профилях бывает старое определение рядом с актуальным, и молча выбранный не тот путь хуже честного "не определился" - pre-check покажет квоту чужой установки, а сверка `session id` не найдет настоящую сессию. Один и тот же путь, повторенный в двух файлах, неоднозначностью не считается (дедуп через `sort -u`);
- **профили читаются грепом, а не `source`** - исполнять профиль ради одного значения лишний риск;
- **`codex doctor` для этого не годится** - он печатает то же эффективное значение и промахивается одинаково с нами.

## Pre-check лимита перед запуском

Цель: не запускать `codex exec`, если окно `primary` на тарифе кончается. Какое это окно, решает тариф: у plus-плана `primary` - **5h** и рядом есть `secondary` (**1w**); на тарифе с одним окном `primary` - недельное, а `secondary` в rollout отсутствует вовсе (наблюдалось 04.09.2026). Метка в сообщениях берется из `window_minutes`, а не пишется руками. Иначе мы тратим минуты ожидания и контекст на ответ, который codex оборвет на полпути по rate-limit.

Источник данных - тот же rollout-лог, что и в "Статистика лимитов после сессии". Берем **последний** rollout-файл по mtime (не привязываемся к session id - его еще нет, мы только собираемся запускать новую сессию). В нем находим последний `token_count` event и из `rate_limits.primary` берем `used_percent` и `resets_at`.

Порог: **`left < 10%`** в окне `primary` - блокировать запуск. Цифра намеренно меньше порога "близко к лимиту" из саммари (20%): 10-20% хватит на средний аудит, а ниже 10% - уже почти гарантированно оборвет.

### Команда (запускать до `codex exec`)

Сначала - резолв домашней папки (см. "Резолв домашней папки codex"), в том же вызове: без него проверка молча выродится в "первый запуск".

```bash
if ! { [ -d "$CODEX_HOME/sessions" ] && [ -r "$CODEX_HOME/sessions" ] && [ -x "$CODEX_HOME/sessions" ]; }; then
  CODEX_HOME=$(grep -hs -E '^[[:space:]]*(export[[:space:]]+)?CODEX_HOME=' \
      ~/.bashrc ~/.bash_profile ~/.profile ~/.zshrc ~/.zshenv 2>/dev/null \
    | sed -E "s/.*CODEX_HOME=//; s/^[\"']//; s/[\"']$//; s|^~|$HOME|; s|\\\$HOME|$HOME|g; s|/+$||" \
    | while IFS= read -r d; do [ -n "$d" ] && [ -d "$d/sessions" ] && [ -r "$d/sessions" ] \
        && [ -x "$d/sessions" ] && echo "$d"; done | sort -u)
  [ -z "$CODEX_HOME" ] && [ -r "$HOME/.codex/sessions" ] && [ -x "$HOME/.codex/sessions" ] \
    && CODEX_HOME="$HOME/.codex"
  [ "$(printf '%s' "$CODEX_HOME" | grep -c .)" -gt 1 ] && CODEX_HOME=""
fi
{ [ -d "$CODEX_HOME/sessions" ] && [ -r "$CODEX_HOME/sessions" ] && [ -x "$CODEX_HOME/sessions" ]; } || CODEX_HOME=""

ROLLOUT=""
[ -n "$CODEX_HOME" ] && ROLLOUT=$(ls -1t "$CODEX_HOME"/sessions/*/*/*/rollout-*.jsonl 2>/dev/null | head -1)
if [ -z "$CODEX_HOME" ]; then
  echo "PRE_CHECK: codex home not resolved - LIMIT NOT CHECKED, ask the user before running"
elif [ -z "$ROLLOUT" ]; then
  echo "PRE_CHECK: no rollout logs found - first codex session on this machine, proceed with warning"
else
  python3 - "$ROLLOUT" <<'PY'
import json, sys, datetime

def fmt_until(s):
    s = int(s)
    if s < 60: return f"{s} sec"
    days  = s // 86400
    rem   = s - days * 86400
    hours = rem // 3600
    mins  = (rem - hours * 3600) // 60
    if days:  return f"{days} day {hours}:{mins:02d} hour"
    if hours: return f"{hours}:{mins:02d} hour"
    return f"{mins} min"

last = None
for line in open(sys.argv[1]):
    try:
        o = json.loads(line)
        p = o.get('payload') or {}
        if p.get('type') == 'token_count' and (p.get('rate_limits') or {}).get('primary'):
            last = p
    except Exception:
        pass
if not last:
    print("PRE_CHECK: no rate_limits event in latest rollout - proceed with warning")
    sys.exit(0)
pr = last['rate_limits'].get('primary') or {}
used = pr.get('used_percent', 0)
left = max(0.0, 100.0 - used)
rt = pr.get('resets_at')
if isinstance(rt, str):
    try:
        rt = datetime.datetime.fromisoformat(rt.replace('Z','+00:00')).timestamp()
    except Exception:
        rt = None
now = datetime.datetime.now(datetime.timezone.utc).timestamp()
reset = fmt_until(max(0, rt - now)) if rt else "unknown"
wm = pr.get('window_minutes') or 0
label = f"{wm//10080}w" if wm and wm % 10080 == 0 else f"{wm//60}h" if wm and wm % 60 == 0 else f"{wm}min"
if left < 10:
    print(f"PRE_CHECK: BLOCKED - {label} window left {left:.0f}% (used {used:.0f}%), reset {reset}")
else:
    print(f"PRE_CHECK: ok - {label} window left {left:.0f}% (used {used:.0f}%), reset {reset}")
PY
fi
```

### Как обрабатывать результат

- `PRE_CHECK: ok ...` - продолжить, запустить codex.
- `PRE_CHECK: BLOCKED ...` - НЕ запускать codex. Вернуть пользователю строку как есть + спросить: "ждать сброса или запустить все равно (рискуя оборваться)?". Не самочинно ставить таймер - решение пользователя. Если пользователь явно скажет "запускай" - запустить, иначе ждать.
- `PRE_CHECK: no rollout ...` / `no rate_limits event ...` - предупредить пользователя ("данных о лимите нет, запускаю") и идти дальше.

### Почему только окно `primary`

На тарифе с двумя окнами длинное (`1w` / `secondary`) тоже отдается в rollout, но один аудит за пару минут не сожжет неделю - там обычно остаток с большим запасом. Блокировать по `secondary` смысла нет: пользователю придется ждать дни до сброса. Если когда-нибудь окажемся в ситуации `secondary < 10%` - вынести отдельно в саммари как warning, но не блокировать запуск.

Лимит общий на все модели и все проекты сразу и тратится быстро при нескольких параллельных прогонах (наблюдалось: недельное окно съедено за 3 дня при 5-8 параллельных прогонах) - бюджет планировать по остатку окна, а не по одной задаче. По той же причине объем задания стоит прикинуть до старта (число единиц работы, не только "фича маленькая/большая"): позднее обнаружение реального объема ломает уже сделанный расчет квоты.

## Статистика лимитов после сессии

Codex CLI пишет лог сессии в `<домашняя папка codex>/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<session_id>.jsonl` (папку резолвить сниппетом из "Резолв домашней папки codex", не собирать путь вручную). Внутри в событиях `token_count` есть блок `rate_limits` с двумя окнами:

- `primary` - короткое окно (для plus-плана `window_minutes=300`, то есть **5h**); на тарифе с одним окном оно единственное и недельное (`10080`).
- `secondary` - длинное окно (для plus-плана `window_minutes=10080`, то есть **1w**); на тарифе с одним окном отсутствует - сниппет печатает только те окна, что есть.

Каждое окно содержит `used_percent` (сколько уже потрачено) и `resets_at` (unix-timestamp, когда счетчик обнулится).

Сами токены (input/cached/output/reasoning) намеренно НЕ собираются - в этом скилле важно одно: насколько мы близко к ограничениям тарифа и когда они сбросятся.

> **Почему не через `/status`.** Слаш-команда `/status` в TUI Codex показывает лимиты, но через `codex exec "/status"` она не работает - модель воспринимает строку как обычный prompt и отвечает текстом про workdir/sandbox без чисел. Отдельного `codex status` subcommand-а тоже нет (`codex --help` не содержит). Поэтому стат собирается из локального rollout-лога, который codex пишет автоматически.

### Команда (запускать сразу после codex exec)

`<report-file>` - отчет твоего режима из таблицы "Режимы" (`audit-report-<topic>` / `consult-report-<topic>` / `escalate-report-<topic>`). Извлечь `session id` из шапки только что созданного отчета, найти соответствующий rollout-файл, достать последний `rate_limits` и дописать блок в конец отчета:

```bash
if ! { [ -d "$CODEX_HOME/sessions" ] && [ -r "$CODEX_HOME/sessions" ] && [ -x "$CODEX_HOME/sessions" ]; }; then
  CODEX_HOME=$(grep -hs -E '^[[:space:]]*(export[[:space:]]+)?CODEX_HOME=' \
      ~/.bashrc ~/.bash_profile ~/.profile ~/.zshrc ~/.zshenv 2>/dev/null \
    | sed -E "s/.*CODEX_HOME=//; s/^[\"']//; s/[\"']$//; s|^~|$HOME|; s|\\\$HOME|$HOME|g; s|/+$||" \
    | while IFS= read -r d; do [ -n "$d" ] && [ -d "$d/sessions" ] && [ -r "$d/sessions" ] \
        && [ -x "$d/sessions" ] && echo "$d"; done | sort -u)
  [ -z "$CODEX_HOME" ] && [ -r "$HOME/.codex/sessions" ] && [ -x "$HOME/.codex/sessions" ] \
    && CODEX_HOME="$HOME/.codex"
  [ "$(printf '%s' "$CODEX_HOME" | grep -c .)" -gt 1 ] && CODEX_HOME=""
fi
{ [ -d "$CODEX_HOME/sessions" ] && [ -r "$CODEX_HOME/sessions" ] && [ -x "$CODEX_HOME/sessions" ]; } || CODEX_HOME=""

SESSION_ID=$(awk -F': ' '/^session id:/ {print $2; exit}' docs/<report-file>.md)
if [ -z "$SESSION_ID" ] || [ -z "$CODEX_HOME" ]; then
  ROLLOUT=""
else
  ROLLOUT=$(ls -1t "$CODEX_HOME"/sessions/*/*/*/rollout-*-${SESSION_ID}.jsonl 2>/dev/null | head -1)
fi

{
  echo
  echo "## Rate limits"
  echo
  if [ -z "$ROLLOUT" ]; then
    if [ -z "$SESSION_ID" ]; then
      echo "no session id in report header - rate limits not collected"
    elif [ -z "$CODEX_HOME" ]; then
      echo "codex home not resolved - rate limits not collected (session id is fine)"
    else
      echo "no rollout file for session $SESSION_ID - rate limits not collected"
    fi
  else
  python3 - "$ROLLOUT" <<'PY'
import json, sys, datetime

def fmt_window(m):
    if m % 10080 == 0: return f"{m//10080}w"
    if m % 1440 == 0:  return f"{m//1440}d"
    if m % 60 == 0:    return f"{m//60}h"
    return f"{m}min"

def fmt_until(s):
    s = int(s)
    if s < 60: return f"{s} sec"
    days  = s // 86400
    rem   = s - days * 86400
    hours = rem // 3600
    mins  = (rem - hours * 3600) // 60
    if days:  return f"{days} day {hours}:{mins:02d} hour"
    if hours: return f"{hours}:{mins:02d} hour"
    return f"{mins} min"

last = None
with open(sys.argv[1]) as f:
    for line in f:
        try:
            o = json.loads(line)
            p = o.get('payload') or {}
            if p.get('type') == 'token_count' and (p.get('rate_limits') or {}).get('primary'):
                last = p
        except Exception:
            pass
if not last:
    print("no rate_limits event found"); sys.exit(0)
models = set()
with open(sys.argv[1]) as f:
    for line in f:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        payload = event.get('payload') or {}
        if event.get('type') == 'turn_context' and isinstance(payload, dict):
            model = payload.get('model')
            if isinstance(model, str) and model.strip():
                models.add(model)
print("model: " + (next(iter(models)) if len(models) == 1 else "unknown"))
rl = last['rate_limits']
now = datetime.datetime.now(datetime.timezone.utc).timestamp()
for key in ('primary', 'secondary'):
    w = rl.get(key) or {}
    if not w: continue
    label = fmt_window(w.get('window_minutes', 0))
    used  = w.get('used_percent', 0)
    left  = max(0.0, 100.0 - used)
    rt    = w.get('resets_at')
    if isinstance(rt, str):
        try:
            rt = datetime.datetime.fromisoformat(rt.replace('Z','+00:00')).timestamp()
        except Exception:
            rt = None
    if rt:
        secs = max(0, rt - now)
        print(f"{label:>4}: left {left:.0f}% (used {used:.0f}%), reset {fmt_until(secs)}")
    else:
        print(f"{label:>4}: left {left:.0f}% (used {used:.0f}%)")
PY
  fi
} >> docs/<report-file>.md
```

Пример того, что получается в конце отчета:

```text
## Rate limits

  5h: left 98% (used 2%), reset 2:35 hour
  1w: left 99% (used 1%), reset 1 day 2:35 hour
```

### Как читать

- `left` - сколько процента квоты тарифа доступно сейчас. Если `< 20%` (для любого из окон, что есть) - вынести в саммари отдельной строкой "близко к лимиту по окну <метка>".
- `reset [D day ]H:MM hour` - компактный таймер до конца текущего окна: целые сутки (если есть), затем часы:минуты с ведущим нулем у минут. Примеры: `2:35 hour`, `1 day 0:00 hour`, `7 day 4:12 hour`, `45 min` (когда меньше часа), `30 sec` (когда меньше минуты). Окна скользящие: `resets_at` в исходных данных - это конец **текущего** окна, не "ровно через 5 часов от сейчас".
- Окна берутся из `window_minutes`. Лейблы `5h` / `1w` подставляются автоматически (300min -> `5h`, 10080min -> `1w`); если codex когда-нибудь сменит тариф/окна - в отчете появятся новые лейблы автоматически, скилл переписывать не надо.

### Если статистика не нашлась

- Файл rollout может не появиться, если codex упал на старте (например, проблемы с авторизацией) или сессия запущена с `--ephemeral`. В этом случае команда напечатает `no rollout file for session <id>` либо (если в шапке отчета нет `session id:`) `no session id in report header`. Это безопаснее, чем подцеплять чужой rollout - статистики просто не будет, и пользователь увидит причину.
- Не использовать `--ephemeral` в этом скилле - теряется возможность собрать статистику.
- Если в шапке отчета нет строки `session id:` - проверь сам план: по этому адаптеру Codex должен первой строкой отчета писать `session id: <id>`. Без этой строки пост-сборка статистики не работает.

## Ограничения

- Codex не получает контекст текущего чата, плана Claude Code, открытых файлов - только то, что в плане и что прочитает сам в репо.
- **`401 Missing bearer` при работающем интерактивном codex - это не протухший логин, а неэкспортированный `CODEX_HOME`**: `codex exec` - дочерний из скилла или запущенный руками из неинтерактивного shell - не увидел переменную из профиля пользователя и ушел в дефолт `~/.codex` без авторизации. Формы одного симптома: отчет из строк `ERROR: unexpected status 401 Unauthorized` + `Reconnecting...` при rc=0, пост-сбор пишет "no rollout file for session" (сессии и не было), `codex login status` отвечает `Not logged in`, `resume` настоящего id - `no rollout found for thread id`. Проверка в две строки: `codex login status` с переменной и без - разные ответы (`Logged in using ChatGPT` против `Not logged in`). Чинится экспортом в том же Bash-вызове (см. "Шаблон вызова"), а не просьбой перелогиниться (правило 9); `bash -lic` с профилем пользователя для этой проверки не брать - профили читаются грепом, а не исполняются (блок резолва). Наблюдалось 08.09.2026: агент предложил пользователю `codex login`, опроверг сам пользователь скриншотом работающего TUI.
- **В не-git проекте codex не стартует, причем молча.** Если `<project-root>` не git-репозиторий и не в trusted-каталогах codex, запуск без `--skip-git-repo-check` завершается с **rc=0** и отчетом ровно из двух строк:

  ```text
  Reading additional input from stdin...
  Not inside a trusted directory and --skip-git-repo-check was not specified.
  ```

  Опасность в том, что это неотличимо от "модель отработала и ничего не нашла": код возврата нулевой, файл отчета создан. Такой отчет за результат не принимать - перезапустить с флагом (правило 10). Статистику лимитов собирать бессмысленно: сессии не было, строки `session id` в отчете нет. Проверено на codex v0.144.x, поймано на vault-проекте без git.
- Read-only означает: codex не правит файлы, не коммитит, не запускает разрушительные команды. Файлы-секреты перекрывает скрипт по шаблонам имен из спеки. Не добавлять в план секреты - они попадут в `<plan-file>.md` (рабочая заметка, в git не едет, но локально лежит).
- Разрешение в `settings.local.json` строго `Bash(python3 <toolkit>/scripts/codex-sandbox.py audit:*)` - без этого префикса вызов потребует подтверждения.
- Скилл намеренно использует синхронный `codex exec`, а не async-плагин `codex-plugin-cc` (`codex-companion.mjs`). Async дает non-blocking, но он Linux-only (нужны `/proc`, `flock`, GNU coreutils) и тащит классы зависаний (зомби-джобы, заклинивший per-cwd broker - в цепочке worker->broker->app-server нет таймаута на запрос), для лечения которых нужна отдельная обвязка. У синхронного `codex exec` этих проблем нет: один foreground-процесс, Bash-таймаут чисто его снимает, чистить нечего. Non-blocking при необходимости берется отвязанным процессом ("Отвязанный запуск - протокол"); `run_in_background` для `codex exec` не используется - харнесс снимает долгие фоновые задачи.

## Как встроить валидацию в поток, чтобы ее не выключили


Второе мнение ловит то, чего свой взгляд не ловит по построению, но платится временем: прогон на `high` думает минутами. Если каждое решение превращается в паузу, валидацию тихо перестают звать - отказываются не от качества, а от простоя. Правила ниже про то, **когда звать, что подавать, на какой глубине и где ставить гейт**; режимы и приемка - в review/SKILL.md; механика resume, фона и pre-check - в этом справочнике.

**Валидируется решение, а не реализация.** На вход - записка на 10-15 строк: что меняем, почему именно так, какая альтернатива отвергнута и чем. Не дифф и не список файлов. Разбор диффа требует перечитать код - это десятки минут на `high`; разбор решения идет на `medium` за минуты и ловит ровно то, ради чего валидацию заводят, - неверную развилку, причем до того, как написана первая строка. Дифф отдается отдельно и только там, где цена ошибки в реализации выше цены ошибки в замысле: миграция боевых данных, проверка полномочий, тонкое место с гонкой.

**Продолжение сессии экономит чтение, но фиксирует уровень.** `resume` наследует модель и effort исходной сессии и переданные игнорирует. Отсюда ловушка, которая не дает ошибки и потому не замечается: серия коротких консультаций, запущенных продолжением глубокого аудита, целиком идет на `high`, хотя по смыслу каждая из них - обычная развилка. Прецедент 01.09.2026: аудит был заведен на `high` заслуженно (17 находок), две следующие валидации решений пошли продолжением и унаследовали его; заметили не по ошибке, а по расходу квоты.

Обмен делается осознанно, и по умолчанию он такой:

- **валидация решения - новой сессией на `medium`.** Записка на 10-15 строк самодостаточна, перечитывать репозиторий модели незачем, а дорог именно уровень, а не повторное чтение;
- **Вне SDD-compliance `resume` - когда вопрос вправду опирается на прежний глубокий разбор** (re-review фиксов, уточнение находки). Тогда унаследованный `high` - осознанная плата, а не побочный эффект.

Обратное соотношение (продолжение выгоднее) бывает на щедром тарифе, где входные токены дороже уровня, - но это надо знать про свою подписку, а не предполагать.

**Уровень называется вслух до запуска, вместе с причиной** - и с тем, шагнул ли ты от дефолта таблицы и почему. Расход виден только постфактум, в статистике лимитов, а унаследованный уровень в отчете выглядит ровно так же, как выбранный (`rules/silent-failure.md`).

**Запуск фоновый.** Ожидание перекрывается работой, а не встает в нее паузой.

**Гейт на мерж, а не на старт.** Реализация начинается сразу, вердикт учитывается перед вливанием в основную ветку. Обратный порядок ("сперва одобрение, потом код") дает тот же результат по качеству и вдвое больший срок: ожидание встает в критический путь, хотя ждать в нем нечего. Риск переделки невелик - записку на 15 строк переписать дешево, а вердикт приходит раньше, чем ветка готова.

**Глубина по необратимости.** При отсутствии назначения MODELS дефолт проверки `medium`; обязательный минимум `high` нужен там, где ошибка не откатывается, - миграции боевых данных, границы доступа и полномочий, схема данных, механика доставки или удаления файлов. Выбранное владельцем назначение выше минимума сохраняется; `low` допускается только выбранным назначением для механического объекта вне compliance; `minimal` запрещен. Иначе упираешься в квоту, и на необратимое ее не остается: после одного глубокого аудита в пятичасовом окне оставалось 43 процента.

**Граница приема.** Он про решения, у которых есть развилка: два допустимых способа, и выбор между ними меняет результат. То, что проверяется фактом - упал тест, вернулся код ответа, сработала команда, - валидировать тратно: там дешевле проверить, чем спросить. Записка "правильно ли я понимаю, что запрос вернет 404" стоит минут ожидания там, где сам запрос занимает секунду.

Ничего из этого не отменяет pre-check лимита перед каждым запуском (правило 8), верификацию находок своими руками и запрет делегировать проверку собственной работы (`rules/subagents-usage.md`): ценность здесь именно в другой модели, а не во втором проходе тем же взглядом.


Read-only Codex не устанавливает зависимости. Недоступные тесты отмечай NOTRUN и оценивай код по доступным материалам; внешние symlinks могут быть недоступны в песочнице. Не расширяй видимость ради теста. Actual-модель берется только из структурных turn_context событий; цитата model в пользовательском тексте не metadata.

В пакет для Codex добавляй просьбу указать session id первой строкой; затем сверяй его со штатным receipt/rollout по протоколу выше. Это вспомогательный указатель для сбора лимитов, не доказательство actual модели и не универсальное требование к другим провайдерам.
