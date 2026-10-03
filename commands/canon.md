---
description: Синхронизировать проект с каноном claude-toolkit (sync canon)
---

Синхронизируй текущий проект с каноном claude-toolkit.

0. До ref, HTTP и source discovery проверь в корне все три journal paths — `.ai-bootstrap/sync.json`, `.ai-bootstrap/migration.json`, `.ai-bootstrap/journal.json`, считая любые ссылки, включая битые. Несколько журналов — остановка без выбора, удаления или recover.
   - Sync journal: без сети и нового ref следуй сохраненной pinned `migrations/ai-sync.prompt.md` и immutable паре `ai-sync.py`/`ai-bootstrap.py` той же операции: check, разрешенный recover, check; установленный `canon-sync` не нужен. Если provenance неизвестна или пару нельзя подтвердить/получить с того же SHA — остановись с диагностикой, не используй latest main. Затем заверши router.
   - Migration journal: только pinned `migrations/ai-layout.prompt.md` и `ai-migrate.py check/recover --root ROOT`. Bootstrap journal: только `bootstrap/bootstrap-ai.prompt.md` и `ai-bootstrap.py check/recover --root ROOT`. Не запускай чужой recover или новое обновление. Недоступный pinned маршрут — остановка без нового main.
   - Без журналов проверь `.AI` и `.ai-bootstrap`, включая файлы и любые ссылки. Поврежденный layout — остановка без сети, legacy fallback или bootstrap. Если любой путь есть, используй `.AI/skills/canon-sync/SKILL.md` и отдельную pinned `migrations/ai-sync.prompt.md`, затем заверши router. Если skill отсутствует, получи точные байты по HTTP: явный HTTP source пользователя, иначе HTTPS `source.base` из `.AI/canon/canon.state.json`, иначе default `https://raw.githubusercontent.com/dewil/claude-toolkit/main` (fork указывает свой source). Соблюдай явные immutable URL/SHA; иначе из source выведи репозиторий, возьми выбранный пользователем ref или `main` и разреши его один раз в полный commit SHA. Передай skill SHA и `PINNED_URL` уже закрепленной версии, не разрешай ref повторно. Локальный клон не ищи. Ошибка сети, source или pinned-пары — остановка до записей.
   - Если `.AI` и `.ai-bootstrap` отсутствуют, проверь `.claude/canon.yaml`. При наличии сообщи, что legacy sync снят и миграцию через `migrations/ai-layout.prompt.md` нужно запросить отдельно; остановись без legacy sync или auto-migration. Без legacy-маркера сообщи, что синхронизировать нечего и новый клиент требует bootstrap через `start.md`; остановись без auto-bootstrap.

Миграция legacy в `.AI` - только по отдельному явному запросу через `migrations/ai-layout.prompt.md`; `/canon` ее не запускает.
