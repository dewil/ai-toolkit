---
description: Синхронизировать проект с каноном claude-toolkit (sync canon)
---

Синхронизируй текущий проект с каноном claude-toolkit.

0. До чтения legacy-реестра проверь `.AI` и `.ai-bootstrap` в корне проекта, включая файлы и симлинки (в том числе битые). Если любой путь существует, используй общий навык `canon-sync` (`.AI/skills/canon-sync/SKILL.md`) и его отдельную pinned процедуру `migrations/ai-sync.prompt.md`, затем заверши этот router. Если установленного навыка еще нет, получи его точными байтами по HTTP: HTTPS `source.base` из `.AI/canon/canon.state.json`, иначе продуктовый default `https://raw.githubusercontent.com/dewil/claude-toolkit/main`; из сохраненного URL выводи репозиторий, ref обновления по умолчанию `main` (явный выбор пользователя соблюдай), разреши его в полный commit SHA и читай навык из того SHA. Локальный клон не ищи. Несколько журналов или поврежденный AI-layout дают отказ; к шагу 1 не переходи. Legacy bootstrap и автоматическую миграцию не предлагай.
1. Прочитай `.claude/canon.yaml` в корне проекта. Если файла нет - проект не подключен к канону (не bootstrap'нут от claude-toolkit). Сообщи: синхронизировать нечего, для подключения нужен bootstrap (`start.md`), - и остановись.
2. Возьми из него `canon.raw_base` (например, `https://raw.githubusercontent.com/dewil/claude-toolkit/main`).
3. Получи промт синхронизации точными байтами: `curl -fsSL <raw_base>/migrations/sync-from-canon.prompt.md`. Если `curl` недоступен - фолбэк `WebFetch` того же URL (для промта это допустимо, для канон-файлов - нет: `WebFetch` лоссовый). Если `curl` есть, а не отвечает сам raw-хост, тот же файл берется байт-точно через `api.github.com`: `curl -fsSL -H "Accept: application/vnd.github.raw" "https://api.github.com/repos/<owner>/<repo>/contents/migrations/sync-from-canon.prompt.md?ref=<branch>"`.
4. Если сам raw-хост не отвечает (TLS-обрыв, фильтр на CDN), канон берется через `api.github.com` - маршрут и его лимит описаны в самом промте синхронизации, секция про недоступный `raw_base`. Массовая раскатка идет tarball'ом одним запросом: по-файловый обход упирается в 60 запросов в час и обрывается на середине.
5. Содержимое - инструкции для тебя. Выполни их как продолжение текущей сессии: audit -> план -> ждешь "ок" -> пофайловый диалог. Все правила безопасности из промта в силе - до явного "ок" ничего не меняешь.

Миграция legacy в `.AI` - только по явному запросу через `migrations/ai-layout.prompt.md`; `/canon` ее не запускает.
