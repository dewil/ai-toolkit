# Установка на macOS: браузер по требованию для удаленного агента

Пошаговая установка схемы "браузер по требованию" на машину с macOS. Итог: удаленный агент (сессия на сервере) сам поднимает и гасит Chrome на этой машине, браузер не висит открытым окном, когда им никто не пользуется. Правила повседневной работы с уже развернутой схемой - `rules/agent-browser.md`; этот файл - только развертывание.

Команды безопасны при повторе: повторный прогон ранбука ничего не ломает и не плодит дублей. Исключение - два заведомо ручных шага: включение Remote Login (Шаг 1) и первичный логин в браузере (Шаг 6); их просто незачем повторять, если они уже сделаны. Значения в угловых скобках подставить из таблицы в конце.

## Зачем так, а не проще

Часть сайтов привязывает сессию к устройству и IP (здесь и далее LinkedIn и hh - примеры таких сайтов, а не обязательная часть схемы). Если перенести куки на сервер и ходить с адреса датацентра, аккаунт получает checkpoint или тихий разлогин. Поэтому браузер обязан оставаться на домашней машине с домашним IP, а сервер только отдает ему команды.

Предыдущая схема требовала руками запускать Chrome и руками поднимать туннель, а после работы окно оставалось висеть - агент погасить его не может, у Chrome DevTools Protocol в используемом клиенте нет закрытия браузера, а последнюю вкладку закрывать запрещено.

## Как устроено

Постоянно на Mac живет только `autossh` - процесс без интерфейса. Он держит обратный туннель к серверу с двумя пробросами:

- `9222` - Chrome DevTools Protocol, по нему агент управляет страницами;
- `2222` - канал управления (SSH на сам Mac), по нему агент запускает и гасит Chrome.

Сервер, подключаясь на свой `127.0.0.1:2222`, попадает по SSH на Mac, но выполнить там может **только** скрипт `chrome-ctl` с одним из четырех аргументов - это обеспечивает forced command в `authorized_keys`. Произвольные команды на Mac по этому ключу невозможны.

Chrome запускается в отдельном профиле (`--user-data-dir`), не в основном: два инстанса на одном профиле Chrome не разрешает, и агентские вкладки не должны смешиваться с личными. По умолчанию режим headless - окна нет вообще; когда нужно смотреть глазами, есть отдельная команда с окном.

```
[Mac]                                        [Сервер]
autossh (постоянно) ──── SSH ───────────────▶ слушает 127.0.0.1:9222 (CDP)
   -R 9222 -R 2222                            слушает 127.0.0.1:2222 (управление)
                                                        │
chrome-ctl ◀──── forced command по ключу ◀──────────────┘
   └─▶ Chrome (headless, профиль ~/.chrome-agent) - живет только на время задачи
```

## Предусловия

- macOS, установлен Google Chrome в `/Applications/Google Chrome.app`.
- С этого Mac есть рабочий SSH на сервер. Нужен только на время установки: им прописывается ключ туннеля, а сам туннель дальше ходит отдельным выделенным ключом (создается в ходе установки).
- Homebrew установлен.
- **Порты `9222`/`2222` на сервере свободны.** Проверить до начала: `ssh <SERVER_USER>@<SERVER_HOST> 'ss -tln | grep -E ":(9222|2222)\b"'`. Заняты - схему уже развернули с другой машины; тогда берем следующую свободную пару (`9223`/`2223`) и подставляем ее везде: в `chrome-ctl`, в `-R` LaunchAgent, в `--port` бэкапа сессии (Шаг 6б), в проверках, а также в `permitlisten` - если применяете суженную строку ключа из конца Шага 5а (базовый блок этого шага ставит строку без `permitlisten`, и порты в ней не упоминаются). Ключи и forced command у каждой машины свои, поэтому схемы сосуществуют. Пошаговый разбор этого случая - `runbook-linux.md`, Шаг 0.

## Шаг 1. Включить Remote Login

Нужен входящий SSH на Mac - без него управляющий канал не заработает.

Через интерфейс: System Settings -> General -> Sharing -> Remote Login -> включить. В "Allow access for" выбрать "Only these users" и оставить только `<MAC_USER>`.

Из терминала (потребует пароль администратора и может быть заблокирован без Full Disk Access у терминала - тогда делать через интерфейс):

```bash
sudo systemsetup -setremotelogin on
sudo systemsetup -getremotelogin   # ожидаем: Remote Login: On
```

## Шаг 2. Установить autossh

```bash
brew install autossh
brew --prefix   # запомнить: /opt/homebrew (Apple Silicon) или /usr/local (Intel)
```

Полный путь к бинарю понадобится в Шаге 5: `$(brew --prefix)/bin/autossh`.

## Шаг 3. Создать скрипт chrome-ctl

```bash
mkdir -p "$HOME/bin"
cat > "$HOME/bin/chrome-ctl" <<'EOF'
#!/bin/bash
# Управление агентским Chrome: start | start-gui | stop | status
# Вызывается локально или по SSH через forced command (аргумент приходит в SSH_ORIGINAL_COMMAND).
set -uo pipefail

PORT=9222
PROFILE="$HOME/.chrome-agent"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

raw="${1:-${SSH_ORIGINAL_COMMAND:-status}}"
cmd="${raw%% *}"          # берем только первое слово - защита от дописанных команд

# Свои процессы опознаем по ДВУМ признакам сразу - порт и профиль. По одному
# порту под шаблон попадает чужой Chrome (старая ручная схема, другой
# инструмент), и тогда stop убил бы его, а start отдал бы агенту чужие вкладки.
own_pids() {
  local pid
  for pid in $(pgrep -f -- "--remote-debugging-port=$PORT" 2>/dev/null); do
    ps -o command= -p "$pid" 2>/dev/null | grep -qF -- "--user-data-dir=$PROFILE" && echo "$pid"
  done
}
running() { [ -n "$(own_pids)" ]; }
# Порт занят кем угодно (в т.ч. чужим браузером) - для диагностики.
port_busy() { curl -sf --max-time 2 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; }
# Наш ли процесс headless (нужно, чтобы start-gui не соврал "already running").
own_headless() {
  local pid
  for pid in $(own_pids); do
    ps -o command= -p "$pid" 2>/dev/null | grep -q -- "--headless" && return 0
  done
  return 1
}
# Сигнал своим процессам. Без xargs: у BSD-версии нет гарантии флага -r,
# а на пустом списке GNU xargs без него выполнил бы kill без аргументов.
signal_own() {
  local pid
  for pid in $(own_pids); do kill "$@" "$pid" 2>/dev/null; done
}

case "$cmd" in
  start|start-gui)
    if running; then
      if [ "$cmd" = "start-gui" ] && own_headless; then
        echo "running headless: сначала stop, потом start-gui"; exit 1
      fi
      echo "already running"; exit 0
    fi
    if port_busy; then
      echo "refusing: порт $PORT занят чужим браузером (не профиль $PROFILE)"; exit 1
    fi
    [ -x "$CHROME" ] || { echo "chrome not found: $CHROME"; exit 1; }
    args=( "--remote-debugging-port=$PORT"
           "--user-data-dir=$PROFILE"
           "--no-first-run"
           "--no-default-browser-check"
           "--disable-session-crashed-bubble" )
    [ "$cmd" = "start" ] && args+=( "--headless=new" )
    nohup "$CHROME" "${args[@]}" >/dev/null 2>&1 &
    for i in 1 2 3 4 5 6 7 8 9 10; do
      sleep 1
      if curl -sf --max-time 2 "http://127.0.0.1:$PORT/json/version" >/dev/null; then
        echo "started ($cmd)"; exit 0
      fi
    done
    echo "failed: CDP did not answer on port $PORT"; exit 1
    ;;
  stop)
    running || { echo "already stopped"; exit 0; }
    # Убиваем ТОЛЬКО свои pid (порт+профиль), а не всех по шаблону порта.
    signal_own
    # Пауза перед kill -9 намеренно длинная: Chrome сбрасывает куки на диск при
    # штатном завершении, и двух секунд ему не хватает. Сайты, которые ротируют
    # session-токен при активности (LinkedIn), после жесткого убийства теряют
    # сессию - свежая кука осталась в памяти, а прежнюю сервер уже отозвал.
    for _ in 1 2 3 4 5 6 7 8 9 10; do running || break; sleep 1; done
    if running; then
      signal_own -9
      sleep 1
    fi
    running && { echo "still running"; exit 1; } || { echo "stopped"; exit 0; }
    ;;
  status)
    if running; then
      curl -sf --max-time 2 "http://127.0.0.1:$PORT/json/version" >/dev/null \
        && echo "running (CDP ok)" || echo "running (CDP not answering)"
    else
      echo "stopped"
    fi
    ;;
  *)
    echo "usage: chrome-ctl start|start-gui|stop|status"; exit 2 ;;
esac
EOF
chmod +x "$HOME/bin/chrome-ctl"
```

Проверить локально:

```bash
"$HOME/bin/chrome-ctl" status    # stopped
"$HOME/bin/chrome-ctl" start     # started (start) - окна быть не должно
"$HOME/bin/chrome-ctl" status    # running (CDP ok)
"$HOME/bin/chrome-ctl" stop      # stopped
```

## Шаг 4. Дать агенту ключ с forced command

Пара ключей управления генерируется **на сервере** (приватная часть остается там и никуда не уезжает) - если ее еще нет, на сервере:

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
# .pub тоже проверяем: приватный ключ без публичной части встречается (потеряли,
# скопировали не все), и тогда awk ниже вернул бы пустоту
[ -f ~/.ssh/<CTL_KEY> ] && [ -f ~/.ssh/<CTL_KEY>.pub ] \
  || ssh-keygen -y -f ~/.ssh/<CTL_KEY> > ~/.ssh/<CTL_KEY>.pub 2>/dev/null \
  || ssh-keygen -t ed25519 -N "" -C "chrome-ctl-<HOST>@server" -f ~/.ssh/<CTL_KEY>
awk '{print $1" "$2}' ~/.ssh/<CTL_KEY>.pub    # это и есть <AGENT_PUBKEY> - тело без комментария
```

(Средняя ветка восстанавливает `.pub` из уцелевшего приватного ключа - перегенерировать пару в этом случае незачем, иначе пришлось бы переписывать `authorized_keys` на Mac.)

Полученное значение (`<AGENT_PUBKEY>`) добавить на Mac в `~/.ssh/authorized_keys` **одной строкой** с ограничениями. Ключевое здесь - `command=`: по этому ключу выполняется только `chrome-ctl`, что бы ни прислала сторона сервера, а `restrict` отключает проброс портов, агента и pty.

```bash
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
touch "$HOME/.ssh/authorized_keys" && chmod 600 "$HOME/.ssh/authorized_keys"

KEYBODY='<AGENT_PUBKEY>'   # только "тип base64", без комментария (см. таблицу)
LINE="command=\"/Users/<MAC_USER>/bin/chrome-ctl\",restrict $KEYBODY"
if grep -qF "$LINE" "$HOME/.ssh/authorized_keys"; then
  echo "уже настроено"
elif grep -qF "$KEYBODY" "$HOME/.ssh/authorized_keys"; then
  # ключ есть, но строка другая - молча добавлять вторую нельзя: старая
  # (например, вовсе без command=) продолжит давать обычный shell
  echo "ВНИМАНИЕ: ключ уже в authorized_keys с другими ограничениями - проверить и заменить строку руками:"
  grep -nF "$KEYBODY" "$HOME/.ssh/authorized_keys"
else
  echo "$LINE" >> "$HOME/.ssh/authorized_keys"
fi
```

Проверка идет в два шага, и это принципиально. Полное совпадение строки - "уже настроено", повтор ничего не делает. Тот же ключ с **другими** ограничениями (без `command=`, с иным путем) - остановка с предупреждением: дописать рядом правильную строку недостаточно, sshd применит первую подошедшую, и серверный ключ сохранит обычный shell на Mac. Такую строку заменяют руками.

Путь в `command=` обязан быть абсолютным - `authorized_keys` не раскрывает `$HOME` и `~`. Он же должен совпадать с реальным домашним каталогом: на managed/сетевом аккаунте `$HOME` бывает не `/Users/<MAC_USER>` - проверить выводом `echo $HOME` и подставить фактический путь.

## Шаг 5а. Отдельный ключ для туннеля (не тот, которым вы ходите на сервер)

Туннель поднимается **выделенным ключом с forced command**, а не тем, которым вы обычно логинитесь. Причин две, и обе обязательные: LaunchAgent не умеет вводить пароль (значит ключ без пароля), а ключ без пароля с обычным доступом равен shell'у на сервере для любого, кто его унесет.

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
[ -f ~/.ssh/agent-tunnel ] && [ -f ~/.ssh/agent-tunnel.pub ] \
  || ssh-keygen -y -f ~/.ssh/agent-tunnel > ~/.ssh/agent-tunnel.pub 2>/dev/null \
  || ssh-keygen -t ed25519 -N "" -C "agent-tunnel@<HOST>" -f ~/.ssh/agent-tunnel
KEYBODY=$(awk '{print $1" "$2}' ~/.ssh/agent-tunnel.pub)   # тип+ключ, без комментария
[ -n "$KEYBODY" ] || { echo "не удалось прочитать ~/.ssh/agent-tunnel.pub"; exit 1; }
LINE="command=\"/usr/bin/false\",restrict,port-forwarding $KEYBODY agent-tunnel"
ssh <SERVER_USER>@<SERVER_HOST> "
  mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
  if grep -qF '$LINE' ~/.ssh/authorized_keys; then
    echo 'уже настроено'
  elif grep -qF '$KEYBODY' ~/.ssh/authorized_keys; then
    echo 'ВНИМАНИЕ: этот ключ уже есть с другими ограничениями - заменить строку руками:'
    grep -nF '$KEYBODY' ~/.ssh/authorized_keys
  else
    printf '%s\n' '$LINE' >> ~/.ssh/authorized_keys && echo 'добавлено'
  fi
"
```

Сравнение идет по **телу ключа** (`тип base64`), а не по всей строке: комментарий - произвольный текст, и по нему тот же самый ключ не опознался бы, а дубль с прежними (небезопасными) ограничениями остался бы действовать. Найденную чужую строку скрипт не трогает: sshd применяет первую подошедшую, поэтому дописать рядом правильную бесполезно - старую надо убрать руками.

**`command="/usr/bin/false"` обязателен, а не украшение.** Одного `restrict` мало: он отключает forwarding, pty, agent и `~/.ssh/rc`, но **выполнение команд не запрещает** - строка `restrict,port-forwarding <ключ>` оставляет владельцу ключа неинтерактивный запуск любых команд. Forced command пробросу портов не мешает: autossh идет с `-N` и команду не запускает. (Если `/usr/bin/false` на сервере нет - подставить свой путь, например `/bin/false`.)

**Что остается открытым даже с forced command:** `port-forwarding` разрешает владельцу ключа любые пробросы через сервер, не только 9222/2222 - и `-R` (слушать порт на сервере), и `-L` (ходить через сервер в его сеть). Ключ без пароля, поэтому его кража дает сетевой pivot. Сузить (OpenSSH 7.8+, проверить `ssh -V` на сервере) - **дополнив** строку, а не заменив в ней `port-forwarding`:

```
command="/usr/bin/false",restrict,port-forwarding,permitlisten="127.0.0.1:9222",permitlisten="127.0.0.1:2222",permitopen="127.0.0.1:1" ssh-ed25519 AAAA... agent-tunnel
```

Здесь `port-forwarding` обязателен (это он включает форвардинг обратно после `restrict`), `permitlisten` ограничивает `-R` двумя нужными точками, а `permitopen` на заведомо неиспользуемый порт фактически запрещает `-L`. Старая версия OpenSSH - оставить как есть и знать про остаточный риск.

## Шаг 5б. Постоянный туннель через LaunchAgent

Перед загрузкой туннеля проверить серверный `GatewayPorts`: при `yes` sshd **принудительно** слушает на всех интерфейсах, и явный `127.0.0.1` в `-R` не спасет - наружу выйдут CDP и SSH к Mac.

```bash
ssh <SERVER_USER>@<SERVER_HOST> '
  sudo sshd -T 2>/dev/null | grep -i "^gatewayports" \
    || sshd -T 2>/dev/null | grep -i "^gatewayports" \
    || grep -rih "^[[:space:]]*GatewayPorts" /etc/ssh/sshd_config /etc/ssh/sshd_config.d/ 2>/dev/null \
    || echo "НЕ ОПРЕДЕЛЕНО"
'
# ожидаем "gatewayports no" (дефолт) или "clientspecified"
```

Что делать с ответом: `no`/`clientspecified` - идти дальше. `yes` - **сначала чинить сервер**, туннель не поднимать. `НЕ ОПРЕДЕЛЕНО` (нет прав на `sshd -T`, конфиг не читается, настройка спрятана в `Include`) - тоже **не поднимать**, а выяснить у администратора сервера: неизвестное значение считаем небезопасным. Проверка стоит здесь именно потому, что после запуска туннеля выяснять поздно - порт уже открыт.

```bash
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$HOME/Library/LaunchAgents/local.agent-tunnel.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.agent-tunnel</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(brew --prefix)/bin/autossh</string>
    <string>-M</string><string>0</string>
    <string>-N</string>
    <string>-o</string><string>ServerAliveInterval=30</string>
    <string>-o</string><string>ServerAliveCountMax=3</string>
    <string>-o</string><string>ExitOnForwardFailure=yes</string>
    <string>-o</string><string>StrictHostKeyChecking=accept-new</string>
    <string>-i</string><string>$HOME/.ssh/agent-tunnel</string>
    <string>-R</string><string>127.0.0.1:9222:127.0.0.1:9222</string>
    <string>-R</string><string>127.0.0.1:2222:127.0.0.1:22</string>
    <string><SERVER_USER>@<SERVER_HOST></string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>/tmp/agent-tunnel.err</string>
  <key>StandardOutPath</key><string>/tmp/agent-tunnel.out</string>
</dict>
</plist>
EOF

launchctl unload "$HOME/Library/LaunchAgents/local.agent-tunnel.plist" 2>/dev/null
launchctl load  "$HOME/Library/LaunchAgents/local.agent-tunnel.plist"
launchctl list | grep agent-tunnel     # ожидаем строку без кода ошибки во второй колонке
```

**Адрес в `-R` указан явно (`127.0.0.1:9222:...`), и это не избыточность.** Голое `-R 9222:...` слушает loopback только при серверном `GatewayPorts no` (дефолт), а явный адрес уважается и при `clientspecified`. Против `GatewayPorts yes` не помогает ни то, ни другое - потому проверка выше идет ДО загрузки туннеля. После загрузки убедиться со стороны сервера, что слушается именно loopback: `ss -tlnp | grep -E ':(9222|2222)'` - адрес обязан быть `127.0.0.1`, не `0.0.0.0` и не `*`. Повторять эту проверку после смены серверного конфига.

Если во второй колонке `launchctl list` ненулевой код - смотреть `/tmp/agent-tunnel.err`. Типовая причина: ключ или его каталог на облачном маунте (Google Drive, iCloud, Dropbox) - при логине маунт может быть еще не готов, и `KeepAlive` уводит autossh в цикл падений. Ключ `~/.ssh/agent-tunnel` должен лежать на локальном диске.

Шаги 5а и 5б проверяют "уже сделано" и безопасны при повторе: ключ не перегенерируется, строка на сервере не дублируется, plist перезаписывается тем же содержимым.

## Шаг 6. Первичный логин в агентский профиль

Профиль `~/.chrome-agent` пустой - в нем нет ни одной сессии. Один раз запустить с окном и войти в нужные сайты (например LinkedIn, hh):

```bash
"$HOME/bin/chrome-ctl" start-gui
```

Войти в аккаунты, при желании поставить "запомнить меня", закрыть окно **не убивая процесс**. Браузер сейчас НЕ гасить: следующий шаг (6б) снимает бэкап кук, и ему нужен живой CDP. Команда `stop` идет там, после дампа.

Дальше сессии живут в профиле, и headless-запуск будет уже залогиненным.

## Шаг 6б. Бэкап сессии: чтобы GUI-логин был последним

Жесткое завершение Chrome теряет куки, обновленные в текущем сеансе, - для сайтов с ротацией токена это значит потерю логина и новый ручной заход. Лечится бэкапом: пока сессия жива, куки снимаются через CDP и складываются на сервер; после потери заливаются обратно без GUI и без пароля.

```bash
python3 scripts/chrome-cookies.py dump --port 9222 --domain linkedin.com   # снять, пока залогинены и браузер жив
python3 scripts/chrome-cookies.py list                                     # что лежит в хранилище
python3 scripts/chrome-cookies.py restore --port 9222 --in ~/.config/browser-sessions/linkedin.com.json
```

`--port` здесь равен паре из предусловий: `9222` - дефолт скрипта, но на второй машине пара другая, и забытый флаг молча уведет дамп к чужому браузеру, перезаписав общий файл по домену.

Хранилище - `~/.config/browser-sessions/`, права 600, вне репозитория и вне синкаемых папок: файл с куками равносилен паролю (`rules/secrets-handling.md`), в чат и артефакты его содержимое не выводится.

Дамп снят - теперь можно гасить браузер:

```bash
"$HOME/bin/chrome-ctl" stop
```

Порядок именно такой: остановленный браузер CDP не отвечает, и `dump` откажет. Погасили раньше времени - поднять `start` (headless достаточно, профиль уже залогинен) и снять дамп. Дальше при любом слете сессии - `restore`, и только если он не помог (сайт инвалидировал токен на своей стороне) звать владельца за `start-gui`.

## Как агенту пользоваться браузером

Правила повседневной работы (не гасить между задачами, штатное завершение, выбор площадки) - `rules/agent-browser.md`. Здесь их не дублируем.

## Шаг 7. Сторож простоя (по желанию)

Страховка на случай, если агент забудет погасить браузер: гасит Chrome, когда к порту 9222 никто не подключен.

```bash
cat > "$HOME/bin/chrome-idle-guard" <<'EOF'
#!/bin/bash
# Гасит агентский Chrome, если два прогона подряд не было активных CDP-соединений.
PORT=9222
STATE="/tmp/chrome-idle-guard.state"
pgrep -f -- "--remote-debugging-port=$PORT" >/dev/null 2>&1 || { rm -f "$STATE"; exit 0; }
if lsof -nP -iTCP:$PORT -sTCP:ESTABLISHED >/dev/null 2>&1; then
  rm -f "$STATE"; exit 0
fi
if [ -f "$STATE" ]; then
  "$HOME/bin/chrome-ctl" stop >/dev/null 2>&1
  rm -f "$STATE"
else
  touch "$STATE"
fi
EOF
chmod +x "$HOME/bin/chrome-idle-guard"

cat > "$HOME/Library/LaunchAgents/local.chrome-idle-guard.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.chrome-idle-guard</string>
  <key>ProgramArguments</key>
  <array><string>$HOME/bin/chrome-idle-guard</string></array>
  <key>StartInterval</key><integer>600</integer>
  <key>RunAtLoad</key><false/>
</dict>
</plist>
EOF

launchctl unload "$HOME/Library/LaunchAgents/local.chrome-idle-guard.plist" 2>/dev/null
launchctl load  "$HOME/Library/LaunchAgents/local.chrome-idle-guard.plist"
```

Порог получается 10-20 минут простоя (два прогона по 600 секунд).

**Сторож опциональный, и не зря.** "Занятость" он определяет по наличию TCP-соединения к CDP в момент опроса - а это не то же самое, что "работа идет". Клиент, открывающий соединение только на время вызова, пауза на размышление агента, ожидание, пока владелец пройдет 2FA в GUI-окне, - для сторожа выглядят простоем, и через 10-20 минут он выполнит `stop`. Ставить его стоит, если браузер регулярно забывают гасить; там, где важны длинные ручные сессии, лучше не ставить или поднять `StartInterval`.

## Проверка после установки

На Mac:

```bash
"$HOME/bin/chrome-ctl" status              # stopped
launchctl list | grep agent-tunnel         # процесс жив
```

Со стороны сервера (это делает удаленный агент, от Mac ничего не требуется):

```bash
ssh -p 2222 -i ~/.ssh/<CTL_KEY> -o StrictHostKeyChecking=accept-new <MAC_USER>@127.0.0.1 status
ssh -p 2222 -i ~/.ssh/<CTL_KEY> <MAC_USER>@127.0.0.1 start
curl -s http://127.0.0.1:9222/json/version   # должен ответить JSON с версией Chrome
ssh -p 2222 -i ~/.ssh/<CTL_KEY> <MAC_USER>@127.0.0.1 stop
```

Признак успеха: `start` отвечает `started (start)`, `curl` возвращает версию, окна на Mac при этом не появляется.

## Эксплуатация и типовые проблемы

| Симптом | Причина и что делать |
|---|---|
| `curl` на 9222 не отвечает, туннель есть | Chrome не запущен - выполнить `start` через управляющий канал |
| SSH на 2222 отказывает | Не включен Remote Login (Шаг 1) или туннель лег - проверить `launchctl list \| grep agent-tunnel` и `/tmp/agent-tunnel.err` |
| После сна Mac все отвалилось | Нормально: autossh переподнимет туннель за 30-90 секунд, Chrome нужно запустить заново |
| `start` отвечает `already running`, но CDP молчит | Остался зависший процесс: `stop`, затем `start` |
| В `/tmp/agent-tunnel.err` - `remote port forwarding failed for listen port 9222` | Порт на сервере уже занят: обычно это старая ручная схема (`ssh` с `RemoteForward 9222`) или зависшая сессия. Найти держателя на сервере (`sudo ss -tlnp \| grep :9222`) и закрыть; autossh подхватит порт сам на следующей попытке |
| `start` отвечает `refusing: порт 9222 занят чужим браузером` | На порту сидит не наш Chrome (старая ручная схема, другой инструмент). `chrome-ctl` считает своим только процесс с нашим профилем и чужой не трогает - ни запускает, ни гасит. Разобраться, кто держит порт (`lsof -nP -iTCP:9222 -sTCP:LISTEN`), и закрыть его владельцем |
| `start-gui` отвечает `running headless: сначала stop, потом start-gui` | Наш браузер уже поднят без окна; смена режима требует перезапуска - `stop`, затем `start-gui` |
| Логин на сайте слетел | Сессия живет в профиле `~/.chrome-agent`; сперва `restore` из бэкапа сессии (см. выше), не помог - повторить Шаг 6 |
| Нужно посмотреть глазами | `start-gui` вместо `start` - откроется обычное окно того же профиля |
| Сайт отвечает `Forbidden` в headless, хотя IP домашний | WAF детектит headless-признаки (UA `HeadlessChrome` у `--headless=new` и др.) - домашний IP не спасает. Пример: career.t1.ru (2026-07-31). Обход - `start-gui` (обычный Chrome того же профиля) или зайти руками |

Ограничение: если Mac выключен или спит, удаленный агент браузером воспользоваться не может - это осознанная плата за то, что весь трафик идет с домашнего IP.

## Таблица подстановки

| Заглушка | Что это | Значение / пример |
|---|---|---|
| `<MAC_USER>` | Логин пользователя на Mac (вывод `whoami`) | например `dwl` |
| `<SERVER_USER>` | Логин на удаленном сервере | тот же, под которым сейчас поднимается туннель руками |
| `<SERVER_HOST>` | Адрес сервера | хост или IP, к которому идет `ssh -R` |
| `<AGENT_PUBKEY>` | Публичный ключ агента для forced command - **только тело**, `тип base64`, без комментария | `ssh-ed25519 AAAA...` - публичная часть пары, которую генерирует сервер; секретом не является, но в шаблоне заменена плейсхолдером. Комментарий отбрасывается намеренно: по нему тот же ключ не опознался бы при повторном прогоне |
| `<CTL_KEY>` | Имя приватного ключа управления НА СЕРВЕРЕ (парный к `<AGENT_PUBKEY>`) | например `chrome-ctl-<HOST>`; приватная часть живет только на сервере и никуда не передается |
| `<HOST>` | Короткое имя этой машины, попадает в имена ключей | `mac`, `mbp`; при второй macOS-машине имена обязаны различаться - иначе ключи и записи `authorized_keys` перепутаются |
