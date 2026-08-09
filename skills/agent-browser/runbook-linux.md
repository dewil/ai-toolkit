# Установка на Linux: браузер по требованию для удаленного агента

Пошаговая установка схемы "браузер по требованию" на машину с Linux (проверено на Ubuntu 24.04). Итог тот же, что у `runbook-mac.md`: удаленный агент сам поднимает и гасит Chrome на этой машине. Отличия от macOS-версии - systemd-юнит вместо LaunchAgent, `apt` вместо Homebrew и путь к бинарю Chrome. Логика (chrome-ctl, forced command, выделенный ключ туннеля) переносится один в один.

Правила повседневной работы с уже развернутой схемой - `rules/agent-browser.md`; этот файл только про развертывание. Значения в угловых скобках - из таблицы в конце.

**Повтор безопасен для конфигурации, но не для работающего браузера.** Установочные команды идемпотентны: ключи не перегенерируются, строки в `authorized_keys` не дублируются, юниты перезаписываются своим же содержимым. Но проверки в Шагах 2 и 7 заканчиваются `stop`, то есть гасят Chrome, если он сейчас поднят. На живой машине повторный прогон делать в момент, когда агент не работает, иначе оборвется его сессия.

## Отличия от macOS-версии

| Что | macOS | Linux |
|---|---|---|
| Автозапуск туннеля | LaunchAgent (`~/Library/LaunchAgents/*.plist`) | systemd-юнит `/etc/systemd/system/agent-tunnel.service` |
| Установка autossh | `brew install autossh` | `apt-get install autossh` |
| Путь к Chrome | `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome` | `/usr/bin/google-chrome-stable` |
| Входящий SSH | включить Remote Login | sshd обычно уже работает |
| Режим с окном | окно берет текущую сессию | нужен `DISPLAY` (плюс `XAUTHORITY`), иначе Chrome не стартует |
| Сторож простоя | LaunchAgent со `StartInterval` | systemd timer |

## Шаг 0. Выбрать порты, если схема на сервере уже есть

**Это первый шаг, а не примечание.** Один сервер обслуживает несколько машин с браузерами, и канонические `9222`/`2222` занимает та, которую развернули первой. Проверять до всего остального:

```bash
ssh <SERVER> 'ss -tln | grep -E ":(9222|2222)\b"'
```

Заняты - берем следующую свободную пару (`9223`/`2223`, дальше `9224`/`2224`) и подставляем ее везде: в `chrome-ctl`, в `-R` юнита, в `permitlisten`, в проверках и **в бэкапе сессии** (`chrome-cookies.py --port`, Шаг 8). Порты разных машин не пересекаются, ключи и forced command у каждой свои - схемы сосуществуют без конфликтов.

Последнее место - самое коварное: у `chrome-cookies.py` дефолт `--port 9222`, и забытый флаг молча уводит дамп на **чужую машину**, а результат пишется в общий файл по домену с перезаписью. Получается и потерянный бэкап, и куки не того профиля.

## Шаг 1. Пакеты

```bash
sudo apt-get install -y autossh
# Chrome из официального репозитория Google
sudo install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | sudo gpg --batch --yes --dearmor -o /etc/apt/keyrings/google-chrome.gpg
sudo chmod 644 /etc/apt/keyrings/google-chrome.gpg
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
  | sudo tee /etc/apt/sources.list.d/google-chrome.list
sudo apt-get update && sudo apt-get install -y google-chrome-stable
google-chrome-stable --version
```

`chromium` из snap брать не стоит: конфайнмент мешает профилю вне домашнего каталога и усложняет CDP.

## Шаг 2. Скрипт chrome-ctl

**Скрипт целиком здесь не приводится - возьми его из `runbook-mac.md` (Шаг 3, блок `cat > "$HOME/bin/chrome-ctl"`)** и внеси три правки ниже. Дублировать полторы сотни строк в двух файлах дороже, чем один переход, но и сделать вид, что скрипт тут есть, нельзя: без него следующая же проверка упрется в `No such file or directory`.

Правки: `PORT` из Шага 0, `CHROME="/usr/bin/google-chrome-stable"` и блок `DISPLAY`/`XAUTHORITY` в ветке `start-gui`:

```bash
    if [ "$cmd" = "start" ]; then
      args+=( "--headless=new" )
    else
      # Режим с окном требует доступа к X-сессии: без DISPLAY Chrome не стартует.
      export DISPLAY="${DISPLAY:-:0}"
      [ -n "${XAUTHORITY:-}" ] || for x in /run/user/$(id -u)/gdm/Xauthority "$HOME/.Xauthority"; do
        [ -f "$x" ] && export XAUTHORITY="$x" && break
      done
    fi
```

Остальное - опознание своих процессов по паре "порт + профиль", длинная пауза перед `kill -9`, отказ при чужом браузере на порту - без изменений; смысл каждого решения разобран в `runbook-mac.md`.

Скрипт кладется в `<HOMEDIR>/bin/chrome-ctl`, владелец `<USER>`, права 755:

```bash
sudo -u <USER> -H mkdir -p ~<USER>/bin
# сюда - правленый скрипт из runbook-mac.md
sudo chown <USER>: ~<USER>/bin/chrome-ctl && sudo chmod 755 ~<USER>/bin/chrome-ctl
```

**`<HOMEDIR>` - фактический домашний каталог `<USER>`, а не всегда `/home/<USER>`.** На машине с LDAP или managed-аккаунтом он может лежать где угодно; `authorized_keys` и systemd `~` не раскрывают, им нужен абсолютный путь. Узнать и держать одно значение на всю установку:

```bash
HOMEDIR=$(getent passwd <USER> | cut -d: -f6); echo "$HOMEDIR"
```

Проверка локально (**гасит браузер, если он сейчас поднят** - на живой машине выполнять, когда агент не работает):

```bash
sudo -u <USER> -H ~<USER>/bin/chrome-ctl status   # stopped
sudo -u <USER> -H ~<USER>/bin/chrome-ctl start    # started (start)
sudo -u <USER> -H ~<USER>/bin/chrome-ctl stop     # stopped
```

**Chrome не запускается от root.** Поэтому и скрипт, и браузер живут под обычным пользователем; при развертывании из-под root не забыть `sudo -u <USER> -H`.

## Шаг 3. Ключ управления с forced command

Пара генерируется **на сервере** (приватная часть остается там), публичная кладется на Linux-машину:

```bash
# на сервере
mkdir -p ~/.ssh && chmod 700 ~/.ssh
[ -f ~/.ssh/chrome-ctl-<HOST> ] && [ -f ~/.ssh/chrome-ctl-<HOST>.pub ] \
  || ssh-keygen -y -f ~/.ssh/chrome-ctl-<HOST> > ~/.ssh/chrome-ctl-<HOST>.pub 2>/dev/null \
  || ssh-keygen -t ed25519 -N "" -C "chrome-ctl-<HOST>@server" -f ~/.ssh/chrome-ctl-<HOST>
awk '{print $1" "$2}' ~/.ssh/chrome-ctl-<HOST>.pub    # это и есть <AGENT_PUBKEY> - тело без комментария
```

Три ветки не украшение: пара на месте - ничего не делаем; уцелел только приватный ключ - восстанавливаем из него публичную часть; нет ничего - генерируем. Голый `ssh-keygen -f` на повторном прогоне спросил бы "Overwrite (y/n)?", а при неинтерактивном запуске мог бы перегенерировать пару - и уже прописанная на Linux-машине строка `authorized_keys` молча перестала бы пускать.

На Linux-машине строка добавляется под `<USER>` с той же двухшаговой проверкой, что на macOS:

```bash
sudo -u <USER> -H bash -c '
mkdir -p ~/.ssh && chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
KEYBODY="<AGENT_PUBKEY>"
LINE="command=\"<HOMEDIR>/bin/chrome-ctl\",restrict $KEYBODY"
if grep -qF "$LINE" ~/.ssh/authorized_keys; then
  echo "уже настроено"
elif grep -qF "$KEYBODY" ~/.ssh/authorized_keys; then
  echo "ВНИМАНИЕ: ключ уже в authorized_keys с другими ограничениями - заменить строку руками:"
  grep -nF "$KEYBODY" ~/.ssh/authorized_keys
else
  printf "%s\n" "$LINE" >> ~/.ssh/authorized_keys && echo "добавлено"
fi
'
```

Сравнение - по телу ключа (`тип base64`), а не по всей строке: комментарий произволен, и по нему тот же ключ не опознался бы. Найденную чужую строку скрипт не трогает: sshd применяет первую подошедшую, поэтому дописать правильную рядом со старой бесполезно - старую убирают руками. Путь в `command=` обязан быть абсолютным: `authorized_keys` не раскрывает `~` и `$HOME`.

## Шаг 4. Выделенный ключ туннеля

Генерируется **на Linux-машине** под `<USER>`, публичная часть уезжает на сервер. Проверка существования - та же трехветочная, по той же причине:

```bash
sudo -u <USER> -H bash -c '
mkdir -p ~/.ssh && chmod 700 ~/.ssh
[ -f ~/.ssh/agent-tunnel ] && [ -f ~/.ssh/agent-tunnel.pub ] \
  || ssh-keygen -y -f ~/.ssh/agent-tunnel > ~/.ssh/agent-tunnel.pub 2>/dev/null \
  || ssh-keygen -t ed25519 -N "" -C "agent-tunnel@<HOST>" -f ~/.ssh/agent-tunnel'

KEYBODY=$(sudo -u <USER> awk '{print $1" "$2}' ~<USER>/.ssh/agent-tunnel.pub)
[ -n "$KEYBODY" ] || { echo "не удалось прочитать agent-tunnel.pub"; exit 1; }
```

Строка на сервере (OpenSSH 7.8+) добавляется с той же двухшаговой проверкой:

```bash
LINE="command=\"/usr/bin/false\",restrict,port-forwarding,permitlisten=\"127.0.0.1:<CDP_PORT>\",permitlisten=\"127.0.0.1:<CTL_PORT>\",permitopen=\"127.0.0.1:1\" $KEYBODY agent-tunnel-<HOST>"
ssh <SERVER> "
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

`command="/usr/bin/false"` обязателен: одного `restrict` мало, он не запрещает выполнение команд. `permitlisten` сужает `-R` до двух наших точек, `permitopen` на заведомо неиспользуемый порт фактически запрещает `-L`.

## Шаг 5. Проверить GatewayPorts ДО поднятия туннеля

```bash
# -C обязателен: GatewayPorts разрешен внутри Match, и без параметров соединения
# sshd -T покажет глобальное значение, не применив ветку Match для нашего логина
ssh <SERVER> 'sudo sshd -T -C user=<SERVER_USER>,host=localhost,addr=127.0.0.1 | grep -i "^gatewayports"'
```

`no`/`clientspecified` - идти дальше. `yes` - чинить сервер, туннель не поднимать. Не определилось (нет прав, конфиг в `Include`) - **не поднимать** и выяснять: неизвестное считаем небезопасным. Проверка стоит именно здесь, потому что после запуска выяснять поздно - порт уже открыт.

Частая причина "НЕ ОПРЕДЕЛЕНО" - `sshd -T` требует root. Ходить за этим ответом под обычным пользователем бессмысленно, сразу брать root-доступ на сервер.

## Шаг 6. systemd-юнит

```bash
sudo tee /etc/systemd/system/agent-tunnel.service >/dev/null <<'EOF'
[Unit]
Description=Reverse SSH tunnel to <SERVER> (agent browser)
After=network-online.target
Wants=network-online.target

[Service]
User=<USER>
Environment=AUTOSSH_GATETIME=0
ExecStart=/usr/bin/autossh -M 0 -N \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes \
  -o StrictHostKeyChecking=accept-new \
  -o IdentitiesOnly=yes \
  -i <HOMEDIR>/.ssh/agent-tunnel \
  -R 127.0.0.1:<CDP_PORT>:127.0.0.1:<CDP_PORT> \
  -R 127.0.0.1:<CTL_PORT>:127.0.0.1:22 \
  <SERVER_USER>@<SERVER_HOST>
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now agent-tunnel.service
# enable --now не перезапускает уже активный юнит: при повторном прогоне с
# новыми портами/сервером процесс продолжил бы жить со старым ExecStart
sudo systemctl restart agent-tunnel.service
journalctl -u agent-tunnel.service -n 20 --no-pager
```

Пояснения к параметрам, которые легко счесть лишними:

- `AUTOSSH_GATETIME=0` - без него autossh при `-M 0` под systemd считает быстрый обрыв фатальным и не перезапускается;
- `IdentitiesOnly=yes` - иначе ssh-агент подсунет первый попавшийся ключ, и туннель пойдет не под тем, под которым мы выдали forced command;
- `ExitOnForwardFailure=yes` - при занятом порте туннель честно падает в лог, а не поднимается наполовину; `Restart=always` потом повторит;
- **системный юнит, а не user-юнит.** На сервере без логина пользователя user-юниты не стартуют, пока не включен lingering, - системный юнит с `User=` проще и надежнее.

## Шаг 7. Приемка со стороны сервера

Именно так этим пользуется агент:

```bash
ss -tln | grep -E ":(<CDP_PORT>|<CTL_PORT>)\b"        # адрес обязан быть 127.0.0.1
ssh -p <CTL_PORT> -i ~/.ssh/chrome-ctl-<HOST> <USER>@127.0.0.1 status   # stopped
ssh -p <CTL_PORT> -i ~/.ssh/chrome-ctl-<HOST> <USER>@127.0.0.1 start    # started (start)
curl -s http://127.0.0.1:<CDP_PORT>/json/version                        # JSON с версией Chrome
ssh -p <CTL_PORT> -i ~/.ssh/chrome-ctl-<HOST> <USER>@127.0.0.1 "id; cat /etc/shadow"   # usage: ...
ssh -p <CTL_PORT> -i ~/.ssh/chrome-ctl-<HOST> <USER>@127.0.0.1 stop     # stopped
```

**Предпоследняя строка - не формальность.** Она должна вернуть подсказку по использованию `chrome-ctl`, а не результат команды. Вернула результат - forced command не работает (ключ попал в `authorized_keys` без `command=` либо сработала более ранняя строка с тем же ключом), и схему в таком виде оставлять нельзя.

Но одной этой проверки мало: `command=` и `restrict` закрывают **разные** каналы. `command=` ограничивает только исполняемую команду и сам по себе не запрещает проброс портов, PTY и agent forwarding; `restrict` запрещает их, но команду не фиксирует. Строка без `restrict` тест выше пройдет, оставив ключу доступ в сеть машины. Поэтому проверяем и вторую половину - обе команды обязаны завершиться **ненулевым** кодом:

```bash
timeout 5 ssh -p <CTL_PORT> -i ~/.ssh/chrome-ctl-<HOST> -o ExitOnForwardFailure=yes \
  -N -L 15555:127.0.0.1:22 <USER>@127.0.0.1; echo "-L rc=$? (ожидается не 0)"
timeout 5 ssh -p <CTL_PORT> -i ~/.ssh/chrome-ctl-<HOST> -o ExitOnForwardFailure=yes \
  -N -R 15556:127.0.0.1:22 <USER>@127.0.0.1; echo "-R rc=$? (ожидается не 0)"
```

Нулевой код здесь означает, что владелец управляющего ключа может ходить через эту машину в домашнюю сеть и открывать на ней слушающие порты - `restrict` в строке отсутствует или перебит более ранней записью.

## Шаг 8. Бэкап сессии

Схема бэкапа общая с macOS (`runbook-mac.md`, "Шаг 6б"), скрипт тот же - `scripts/chrome-cookies.py`. Пропускать шаг не стоит: это единственное, что избавляет от повторного ручного логина с 2FA, когда профиль придется поднимать заново.

Порядок здесь обратный приемке: сначала поднять браузер **с окном** и залогиниться руками, и только потом снимать дамп - на чистом профиле снимать нечего.

```bash
ssh -p <CTL_PORT> -i ~/.ssh/chrome-ctl-<HOST> <USER>@127.0.0.1 start-gui   # окно на домашней машине
# зайти на нужный сайт руками, пройти 2FA
python3 scripts/chrome-cookies.py dump --port <CDP_PORT> --domain <ДОМЕН>
python3 scripts/chrome-cookies.py list
```

**`--port` обязателен, если пара нестандартная.** Дефолт скрипта - `9222`; на второй машине забытый флаг подключит дамп к первой, а результат уйдет в общий файл по домену с перезаписью - и правильный бэкап пропадет, и в хранилище лягут куки чужого профиля.

Хранилище (`~/.config/browser-sessions/`) держать вне репозитория и вне синкаемых папок, права 600. Содержимое дампа - секрет, эквивалент пароля (`rules/secrets-handling.md`).

## Шаг 9. Сторож простоя (по желанию)

Страховка на случай, если агент забудет погасить браузер. Логика та же, что в macOS-версии, отличается механика запуска (systemd timer) и способ увидеть активные соединения (`ss` вместо `lsof`):

```bash
sudo -u <USER> -H tee ~<USER>/bin/chrome-idle-guard >/dev/null <<'EOF'
#!/bin/bash
# Гасит агентский Chrome, если два прогона подряд не было активных CDP-соединений.
PORT=<CDP_PORT>
# Файл состояния - в приватном runtime-каталоге пользователя (режим 0700), а не в
# /tmp: там предсказуемое имя может занять другой пользователь машины, и тогда
# сторож либо гасит браузер с первого же прогона, либо не срабатывает никогда.
RUNDIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
[ -d "$RUNDIR" ] || exit 0        # нет runtime-каталога - молча не работаем, но и не гасим
STATE="$RUNDIR/chrome-idle-guard.state"
pgrep -f -- "--remote-debugging-port=$PORT" >/dev/null 2>&1 || { rm -f "$STATE"; exit 0; }
# Ошибку ss трактуем как "не знаем" и ничего не гасим: fail-open здесь означал бы
# гарантированную остановку живого браузера на системе с урезанным netlink.
CONNS=$(ss -tnH state established "sport = :$PORT") || { rm -f "$STATE"; exit 0; }
if [ -n "$CONNS" ]; then
  rm -f "$STATE"; exit 0
fi
if [ -f "$STATE" ]; then
  "$HOME/bin/chrome-ctl" stop >/dev/null 2>&1
  rm -f "$STATE"
else
  touch "$STATE"
fi
EOF
sudo chmod 755 ~<USER>/bin/chrome-idle-guard
```

Юнит и таймер:

```bash
sudo tee /etc/systemd/system/chrome-idle-guard.service >/dev/null <<'EOF'
[Unit]
Description=Idle guard for agent Chrome

[Service]
Type=oneshot
User=<USER>
ExecStart=<HOMEDIR>/bin/chrome-idle-guard
EOF

sudo tee /etc/systemd/system/chrome-idle-guard.timer >/dev/null <<'EOF'
[Unit]
Description=Run chrome-idle-guard periodically

[Timer]
OnBootSec=10min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now chrome-idle-guard.timer
systemctl list-timers chrome-idle-guard.timer --no-pager
```

Порог получается 10-20 минут простоя (два прогона по 10 минут).

**Сторож опциональный, и не зря.** "Занятость" он определяет по наличию TCP-соединения к CDP в момент опроса - а это не то же самое, что "работа идет". Клиент, открывающий соединение только на время вызова, пауза на размышление агента, ожидание, пока владелец пройдет 2FA в GUI-окне, - для сторожа выглядят простоем, и через 10-20 минут он выполнит `stop`. Между снимком соединений и самим `stop` остается зазор: агент, подключившийся в этот момент, получит оборванную сессию. Ставить сторож стоит, если браузер регулярно забывают гасить; там, где важны длинные ручные сессии, лучше не ставить или увеличить `OnUnitActiveSec`.

Второе ограничение: `RUNDIR` существует, пока у пользователя есть сессия. На машине без логина (или без `loginctl enable-linger <USER>`) каталога нет, и сторож штатно выходит, ничего не гася.

> Шаги 1-7 обкатаны живой установкой на Ubuntu 24.04. Шаги 8 и 9 собраны по аналогии с macOS-версией и вживую не проверялись - при первом прохождении сверяйтесь с результатом, а не только с текстом. Для шага 9 проверить после включения таймера: `systemctl list-timers` показывает следующий запуск, а `journalctl -u chrome-idle-guard` - что прогон отработал без ошибок.

## Типовые проблемы (сверх общих из macOS-ранбука)

| Симптом | Причина и что делать |
|---|---|
| `start` отвечает `failed: CDP did not answer`, в логе Chrome про root | Chrome не запускается от root - запускать под `<USER>` (`sudo -u <USER> -H`) |
| `start-gui` не открывает окно | Нет `DISPLAY`/`XAUTHORITY` либо на машине нет графической сессии: `systemctl get-default` должен быть `graphical.target`, `loginctl list-sessions` - показывать сессию |
| Юнит `active`, но порта на сервере нет | Порт занят другой машиной - смотреть `journalctl -u agent-tunnel`, там будет `remote port forwarding failed`; вернуться к Шагу 0 и взять свободную пару |
| Туннель поднимается не тем ключом | Нет `IdentitiesOnly=yes`, ssh-агент подставил другой ключ |
| Сторож гасит браузер посреди работы | Соединение к CDP держится не постоянно - увеличить `OnUnitActiveSec` или выключить таймер (`systemctl disable --now chrome-idle-guard.timer`) |

## Таблица подстановки

| Заглушка | Что это | Пример |
|---|---|---|
| `<USER>` | Пользователь на Linux-машине, под которым живут Chrome и туннель | `dwl` |
| `<HOMEDIR>` | Фактический домашний каталог `<USER>` (`getent passwd <USER>`) - нужен там, где `~` не раскрывается: `authorized_keys`, systemd | `/home/dwl` |
| `<HOST>` | Короткое имя машины, попадает в имена ключей | `home` |
| `<CDP_PORT>` | Порт Chrome DevTools Protocol на сервере | `9222`, при занятости `9223` |
| `<CTL_PORT>` | Порт управляющего канала на сервере | `2222`, при занятости `2223` |
| `<SERVER_USER>` / `<SERVER_HOST>` | Логин и адрес сервера, куда идет туннель | `dwl` / `llm.example.ru` |
| `<SERVER>` | SSH-алиас сервера для команд из ранбука | `llm` |
