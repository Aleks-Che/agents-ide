# Журнал реализации плана

Связан с [планом реализации](IMPLEMENTATION_PLAN.md). Здесь фиксируются выполненные изменения, нюансы разработки, принятые решения, блокеры и проверки, которые важно учитывать при продолжении работы.

## Как вести журнал

- Перед началом этапа читать открытые вопросы и записи предыдущих этапов. После значимого изменения или завершения рабочего прохода добавлять запись по шаблону ниже, новые записи — сверху.
- Указывать номер этапа, результат, причины нетривиальных решений, ограничения и следующий шаг. Для временного обходного решения записывать, когда его можно убрать.
- Различать реализованную возможность, запланированную работу и `unverified`. Наличие кода, mock или успешного транспорта само по себе не подтверждает реальную интеграцию.
- Для блокера указывать, какую работу он останавливает и что должно произойти для снятия. При снятии обновлять таблицу и добавлять запись с доказательством; прежние наблюдения сохранять.
- Проверки записывать с результатом и областью действия. Отдельно отмечать пропуски, непроверенные среды и отсутствие удалённого CI-прогона.
- Если изменился контракт, обновлять соответствующий архитектурный документ и ссылаться на него. Чекбоксы готовности остаются в плане, подробные capability и fixtures — в матрице интеграций.
- Не включать ключи, pairing-коды, cookie, содержимое SecretStore и необезличенные ответы harness.

## Открытые вопросы и ограничения

Состояние на 2026-09-14 после ревью этапов 2, 2A, 3 и 4. Fake-движок выполняет критерий repair-цикла; интеграция resume/recovery остаётся открытой до этапа 5. Допуски к реальному исполнению проверяются отдельно.

| ID | Статус | На что влияет | Что проверить или сделать |
| --- | --- | --- | --- |
| B-001 | Блокер допуска автономной записи; `unverified` | Роли с записью в 6A/6B | Подтвердить границы разрешённой записи и отрицательные сценарии выхода за workspace. Успешный отказ permission не доказывает изоляцию разрешённого действия. До проверки — `no-go` для обоих harness. |
| B-002 | Блокер автоматического восстановления; `unverified` | Этапы 5, 6A/6B | Проверить смерть процесса и обрыв транспорта во время незавершённого внешнего действия. Восстановление завершённой истории не разрешает повтор запроса с неизвестным исходом. |
| V-001 | `unverified` | Адаптер OpenCode, этап 6A | Проверить ошибку авторизации именно провайдера и полный поток model delta. Сейчас подтверждены 401 локального server и SSE `server.connected`. |
| V-002 | Удалённый прогон не выполнен | Приёмка изменений через CI | Выполнить добавленный GitHub Actions workflow. Локальные Windows-тесты прошли; они не означают, что Windows/Linux jobs уже прошли на GitHub. |
| V-003 | Частично проверено | Пути и резервации, этап 5 | В ревью этапа 2 проверены junction-алиас, вложенные области, общий checkout и подмена каталога. Остаются финальные file handles/TOCTOU при исполнении, subst/8.3 и длинные пути всей цепочки Python/Git/harness. Отказ сетевого диска проверен имитацией Win32 DRIVE_REMOTE, без реального сетевого тома. |
| V-004 | Политика пока не допускается | Git, этап 8 | Не включать `dirty_policy=allow_nonoverlap` до проверок baseline, allowlist, временного index, hooks/signing и внешних изменений. |
| V-005 | Замеры не выполнены | Нагрузочная приёмка, этап 12 | Измерить зафиксированный профиль 100 000 событий на Windows/NTFS/SSD после появления журнала Run и артефактов. Цели p95 пока являются критериями, а не результатами. |
| V-006 | Условие обновления старых данных | Базы с записями из первоначального этапа 2 | В 0002 не сохранялись исходный запрос Run, payload команд и полный scope резервации. Миграция 0003 сохраняет историю, но не выдумывает утраченные поля. Для legacy-старта возвращается `idempotency_unverifiable`; команды с NULL payload и неизвестные резервации требуют сверки на этапе 5. Новые записи содержат все эти поля. |
| V-007 | Совместимость ранних снимков групп | Run из первоначальной 0004, этапы 4–5 | Снимок мог хранить ID кандидатов без всех settings, endpoint и версий секретов. 0005 не переписывает историю и не достраивает её из текущих ресурсов. Будущий worker должен отказать исполнению такого неполного снимка; новый формат маркирован `dependencies.model_selection_version=1`. Старые params с credentials/управлением не выдаются и не экспортируются: требуется исправление состава через PUT. |

Основания и точные границы: [матрица интеграций](integrations/CAPABILITIES.md), [решения каркаса](architecture/FOUNDATION_DECISIONS.md), [runtime-контракты](architecture/RUNTIME_CONTRACTS.md).

V-008 — интеграционная часть этапа 3 закрыта частично: движок 4 передаёт cycle/scope, сохраняет loops/assignments/счётчики и повторяет directory/resource/secret-проверки под резервацией. Для явного simulated допустим dispatch_ready=true. Реальные адаптеры ещё должны подтвердить точные capability, формат/контекст, бюджеты и permissions; Git — baseline/allowlist/hooks, evidence — версию файлов. Для real dispatch_ready=false; объявлять весь этап 3 закрытым пока нельзя.

V-009 — совместимость ранних графов этапа 3: ранее принимались неоднозначные переходы, циклы без лимитов и неизвестные поля. Теперь публикация/старт их отклоняют. Миграция 0006 сохраняет историю и добавляет только origin; исправление требует новой версии. Новые итоговые хэши включают фактические inputs, старые snapshot/hash не переписываются.

V-010 — закрыт ревью этапа 4: полный impl → verifier failed → Condition(false) → impl repair → verifier passed → Condition(true) → End проверен через API и worker. Проверены смена промпта/feedback, два visit/cycle, decision=false/true и лимиты. Никаких when на не-Condition узле или переноса этой проверки в этап 8 не требуется.

V-011 — открытая интеграция этапов 4–5: runtime checkpoint, позиция кандидата, retry_at, счётчики и история сохранены; resume/resolve/reconciliation и явный повторный проход группы пока не реализованы. Повтор execute не начинает Start заново, потеря владельца ведёт в recovering, неизвестный исход не разрешает fallback. Для начатого/приостановленного Run неподдержанные команды отвечают control_unimplemented; waiting_input не рекламирует доступное продолжение. Полные interrupt/kill/ProcessSupervisor и гонки управления закрываются на этапе 5.

V-012 — старые данные первоначального этапа 4: lease-изменения могли не сохраниться, StepAttempt/StepExecution остаться running, а body артефакта потеряться. Миграция 0007 сохраняет историю, добавляет checkpoint/selection/body и не выдумывает утраченные результаты. Старое посещение без checkpoint не повторяется; задания без владельца требуют recovering. Body старого manifest может быть null. Для сверки этих Run нужен этап 5, удалять историю или переотправлять вызовы автоматически нельзя.

## Записи

### 2026-09-14 · Этап 4 · Ревью движка, очереди и SSE

**Статус:** критические дефекты исправлены, fake-критерий выполнен; один интеграционный пункт resume возвращён в открытое состояние (V-011). Исходные 197 тестов проходили, но не проверяли настоящий repair-цикл, durable lease/результаты, бюджет и опасный fallback. Первые новые fixtures потребовали корректировки ожиданий HTTP 201; итоговые проверки проверяют сохранённые эффекты и точные состояния.

| Найденное несоответствие | Исправление |
| --- | --- |
| SQL COMMIT выполнялся до flush ORM: claim/refresh/release теряли изменения; terminal/waiting Run можно было запускать повторно | ORM commit под BEGIN IMMEDIATE; атомарные lease/generation/reservation, фильтр состояния, лимит 2 Run; очередь потребляется вместе с остановкой/завершением. |
| Lease не ограничивал записи Runner; heartbeat worker останавливался на долгом вызове | Проверки владельца/резервации/срока на каждой транзакции и перед commit, abort при неудачном продлении; отдельный heartbeat и два рабочих потока. Истечение ведёт в recovering. |
| Condition/assignments видели latest={}, посещения не завершались, результат попытки менялся в detached ORM-объекте | Результат перечитывается в новой сессии, status/result/decision/attempt_count сохраняются; latest только succeeded текущего cycle/scope; cursor/assignments/переход атомарны. |
| Счётчики жили в памяти, Condition-loop не учитывался, сценарий не различал повторные visits | Миграция 0007, runtime checkpoint с usage/loops/retry_at/кандидатом; лимиты проверяются перед действием. Сценарий адресует visit_index/attempt_index. |
| Unknown/transport/process errors автоматически повторялись; permission/invalid JSON переключали провайдера | Retry/fallback требуют safe + no_effect; ошибки формата/permission/конфигурации имеют свои политики; unknown блокирует новые вызовы. Retry считает политику отдельно для кандидата и сохраняет backoff. |
| Любой обычный Run молча исполнялся fake; неготовые Command/Git/PlanControl считались успешными | Явный execution_mode=simulated, сценарий и режим в доверяемом hash; real и неготовые исполнители блокируются. Fake-правки только в отдельном каталоге, без сети/Command. |
| output_schema игнорировалась, failed/unknown смешивались со статусом вызова | Сервер разбирает и валидирует JSON/verdict, сохраняет boolean/null decision; бизнес-failed не является технической ошибкой. Unknown имеет отдельный маршрут. |
| ArtifactManifest.body не был колонкой; секреты во вложенном JSON не очищались; байтовый cap нарушался на Unicode | Сохранённый body_json, рекурсивная redaction, валидный JSON при усечении, реальные input/prompt/result/assignments и связанные ID попыток. Event cap 16 KiB, большие payload через artifact_id. |
| SSE повторно использовал закрытую сессию БД, терял страницы terminal Run, игнорировал Last-Event-ID/reset/auth revocation | Короткие согласованные чтения, snapshot/cursor, один poller на Run, id/reconnect, pagination/deduplication, heartbeat, буфер 1 MiB и отзыв cookie-сессии. |
| API-каталог событий расходился с Runner, текстовые события не передавались | Единый каталог и генерируемый events.json/TypeScript envelope, callback delta с немедленной фиксацией и привязкой к исходной попытке. |

**Проверки и приёмка:** добавлены 44 регрессионных случая в [test_stage4_review](../backend/tests/integration/test_stage4_review.py) и две проверки реального worker в [test_stage4_engine_runner](../backend/tests/integration/test_stage4_engine_runner.py). Покрыты настоящий repair, unknown, структурированный ответ, retry/fallback/лимиты, истечение lease, конкурентный claim, артефакты, timeout и запрет поздней правки, SSE >200 событий/retention/отзыв сессии и старые visits без checkpoint. Ранее названные repair/limit-тесты переименованы по действительным утверждениям; пустой граф не считается тестом ограничения бюджета.

Итоговый полный прогон на Windows/Python 3.12.7: **243 passed**, 2 прежних предупреждения Starlette/httpx и anyio, 137,09 секунды (197 исходных + 46 новых). Ruff check/format — без замечаний, mypy — 60 модулей для Windows и Linux target. OpenAPI, node/event JSON и TypeScript синхронизированы. Frontend ESLint/Prettier, 2 Vitest, 1 Playwright pairing/reload/revoke и TypeScript/Vite build прошли. Финальный wheel собран, переустановлен в отдельное чистое окружение и мигрировал новую БД до 0007. Это локальные проверки Windows; выполнение Linux runtime и удалённого CI не заявляется.

**Контракт и продолжение:** [ENGINE_RUNTIME](architecture/ENGINE_RUNTIME.md), [миграция 0007](../backend/src/agents_ide/persistence/migrations/versions/0007_engine_review.py). Снимки не переписываются. Поддержаны pause/stop/cancel на безопасных границах; при неизвестном исходе отмена не подтверждается. В этапе 5 реализовать полноценные команды, recovery и продолжение checkpoint; в 6–8 — реальные адаптеры, evidence и Git. UI graph/run и нагрузочная retention-приёмка ещё впереди. Реальные платные вызовы и удалённый GitHub CI в ревью не запускались.

### 2026-09-14 · Этап 4 · Движок на fake-исполнителе

**Исторический отчёт до ревью:** заявлялось завершение этапа, 11 новых тестов и 197 passed. Утверждения о repair, durable lease/результатах, безопасном fallback и SSE были неполными; актуальные исправления и границы приведены в записи выше. Текст ниже сохранён как история первоначального прохода.

**Реализовано.**

- [`adapters/base.py`](../backend/src/agents_ide/adapters/base.py) — контракты `AgentAdapter`/`LLMAdapter` и общие обёртки `AgentResult`/`LLMResult` с `ExternalOutcome`. Синхронные адаптеры: реальные harness в этапе 6 будут оборачивать свои потоки.
- [`adapters/fake.py`](../backend/src/agents_ide/adapters/fake.py) — `FakeAgentAdapter`/`FakeLLMAdapter` со сценарием, маркировкой `simulated` и опцией `force_decision=` в промпте для тестов.
- [`engine/runner.py`](../backend/src/agents_ide/engine/runner.py) — основной `Runner`: последовательное выполнение, выбор перехода (с поддержкой Condition), применение assignments, выбор кандидата по snapshot, события, артефакты, terminal/waiting состояния, лимиты визитов/вызовов/обратных переходов/времени.
- [`engine/candidates.py`](../backend/src/agents_ide/engine/candidates.py) — выбор первого доступного кандидата, пропуск отключённых/архивных/несовместимых, forward-only fallback, исключение подтверждённо отказавших в текущем посещении.
- [`engine/visits.py`](../backend/src/agents_ide/engine/visits.py) — управление `visit_index`, `cycle_id`, `StepExecution`/`StepAttempt`, последние результаты для AST.
- [`engine/events.py`](../backend/src/agents_ide/engine/events.py) — каталог событий и `MAX_PAYLOAD_BYTES` для payload-bounded событий.
- [`engine/artifacts.py`](../backend/src/agents_ide/engine/artifacts.py) — `ArtifactManifest`, sanitization секретов и bounded payload.
- [`engine/queue.py`](../backend/src/agents_ide/engine/queue.py) — `BEGIN IMMEDIATE` claim/refresh/release с поколением и lease; реализует короткие транзакции этапа 2.
- [`engine/events_stream.py`](../backend/src/agents_ide/engine/events_stream.py) — fetch_events_after, fetch_full_history, format_sse, reset_required/cookie-auth.
- [`worker/main.py`](../backend/src/agents_ide/worker/main.py) — воркер теперь реально claim-ит задания и запускает `Runner` в фоне с продлением lease.
- [`api/domain.py`](../backend/src/agents_ide/api/domain.py) — добавлены `/api/runs/{id}/events`, `/events/replay` и `/stream` (SSE с cookie-auth и reset_required).
- [`tests/integration/test_stage4_engine_runner.py`](../backend/tests/integration/test_stage4_engine_runner.py) — 11 интеграционных тестов: idempotent start, попытка/артефакт, candidate_selected для direct и group, SSE-replay, waiting_input при исчерпании, candidate_skipped/switched.

**Нюансы и решения.**

- `_current_work_state` для `_record_artifact` восстанавливает work_state из snapshot для совместимости с поздним добавлением `_record_artifact`. Для повторных прогонов run work_state хранится в snapshot.
- `_select_transition` пока поддерживает `when` только на ребрах от `Condition`; для других узлов маршрут берёт первое ребро. Расширение под repair-цикл оставлено пресету этапа 8 (PlanControl).
- `_normalize_decision` сопоставляет `passed`/`true`/`ok` → passed, `failed`/`false` → failed, `unknown`/`inconclusive` → unknown.
- `_exhausted` помещает Run в `waiting_input(model_group_exhausted)` с причинами каждого кандидата и сохранением счётчиков; resume допускается только явный новый проход списка.
- Сценарий fake-адаптера встроен в snapshot через ключ `fake_scenario` (вводится через `FakeScenario`). Текущий API не позволяет встраивать его через HTTP — это сознательное ограничение, чтобы тесты не зависели от production-API; детальный сценарий передаётся напрямую через адаптер.

**Проверки.**

- Backend на Windows/Python 3.12.7: **197 passed** (186 предыдущих + 11 новых). Добавлены тесты:
  - `test_runner_drive_completed_graph_on_fake` — выполняет граф impl→check→End через реальный worker;
  - `test_runner_records_attempt_and_artifact` — события attempt.started/finished и artifact.recorded;
  - `test_runner_sse_stream_returns_events_then_closes` — поток событий через cookie-auth SSE;
  - `test_runner_idempotency_returns_same_run` — повторный `idempotency_key` возвращает тот же Run;
  - `test_runner_pause_command_rejected_until_started` — журнал команд пуст до старта;
  - `test_runner_writes_candidate_selection_events` — `model_group.candidate_selected` для direct;
  - `test_runner_selects_first_group_member` — для группы `heavy` (2 кандидата) выбирается первый;
  - `test_runner_records_repair_after_failed_verification` — verifier с `force_decision=failed` завершает visit и помечает decision;
  - `test_runner_emits_replay_endpoint` — `/events/replay` отдаёт историю с `reset_required=true`;
  - `test_runner_records_artifact_for_attempt` — `ArtifactManifest` создаётся для успешной попытки;
  - `test_runner_terminates_on_limit_exceeded` — Run с минимальным графом завершается корректно.
- `ruff check src/ tests/` без замечаний. `mypy src/` и `--platform linux` зелёные.
- `scripts/generate_contracts.py --check` подтверждает синхронность OpenAPI/TS.

**Границы и продолжение.** Этап 5 (управление, lease/heartbeat/recovery, ProcessSupervisor) запускается следующим: кандидат selection уже работает, retry/fallback выполняется, но worker всё ещё не реагирует на команды `pause`/`stop`/`cancel` во время выполнения — это добавляется в этапе 5 вместе с восстановлением прерванных Run. Реальные harness (Codex/OpenCode) относятся к этапам 6A/6B; подключение LLM и Command — к этапу 7; пресет — к этапу 8; UI и наблюдение — к этапам 9–11.

### 2026-09-14 · Этап 3 · Ревью схем, AST, переходов и preflight

**Статус:** дефекты конфигурационной реализации исправлены; два интеграционных чекбокса возвращены в открытое состояние (V-008). Исходный отчёт с 142 тестами ниже сохранён как история до ревью.

| Найденное несоответствие | Исправление |
| --- | --- |
| JSON Schema выдавалась клиенту, но Command/CollectContext/PlanControl почти не проверялись; не-объекты silently пропускались | Сервер применяет те же закрытые схемы через зафиксированный jsonschema, ограничивает размер/глубину, проверяет поля команд, источников и операций. |
| `exists` возвращал само значение, сравнение разных типов превращалось в false, `not` без operand давал 500 | Presence-семантика exists, ошибки типов, строгий bounded AST, проверка полей и единый диагностический ответ. |
| Ссылки зависели от порядка узлов; prompts/exists обходили проверки; missing молча удалялся | Полный индекс ID, проверка scopes/полей/входов, однопроходная подстановка с ошибкой missing, cycle/scope/manifest-фильтрация LatestResult. |
| Обычные узлы могли ветвиться, циклы не имели лимитов, недостижимые узлы принимались | Ровно один переход либо true/false/unknown у Condition; достижимость всех узлов/End, проверка DAG после удаления явно ограниченных loops. |
| Assignments отсутствовали и не входили в hash; edge labels меняли hash | Ограниченные work-присваивания, атомарное вычисление RHS, hash с лимитами/assignments и без оформления. |
| Импорт терял schema_version/settings, принимал неизвестные features и не требовал доверия | Полный переносимый документ, отказ неизвестных полей/версий/features, origin=imported, миграция 0006, подтверждение итогового hash при Start. |
| Preflight игнорировал binding и Run overrides/inputs, не выдавал итоговую конфигурацию | Общий resolver, все кандидаты/параметры/источники/назначения, DPAPI-проверка заданных секретов, программы/пути, Git-план. Start проверяет те же inputs/overrides. |
| Группа со всеми недоступными кандидатами проходила; неподдержанные параметры и бюджеты молча принимались | Отсутствие доступных кандидатов — blocker; общие типы/диапазоны параметров и строгие измеримые лимиты проверяются, реальные capability остаются unverified. |
| Hash запуска не учитывал переопределённые входы, включая список команд | Итоговый execution_hash включает фактические inputs; подмена входов после подтверждения требует нового доверия. |

**Контракт:** [GRAPH_VALIDATION](architecture/GRAPH_VALIDATION.md). Импортированный черновик сохраняет origin при правке без этого поля. Передача `trusted=true` из файла ничего не подтверждает. Read-only preflight не запускает модель/Command, не меняет Git и не резервирует workspace. Git-пробы Start остаются перед короткой write-транзакцией. API доступен через прежние pairing/CSRF.

**Проверки:** исходные 142 теста прошли; первые 16 регрессионных случаев воспроизвели ошибки до исправления. Финальный Windows/Python 3.12.7 прогон: **186 passed**, 2 прежних предупреждения Starlette/httpx и anyio, 77.87 секунды. Добавлены **44 случая** в [test_stage3_review.py](../backend/tests/integration/test_stage3_review.py). Два прежних теста ошибочно ожидали успех неоднозначного графа/цикла без лимита; ожидания исправлены. Сценарий переопределения LLM теперь действительно запускает LLM-узел, а не неназначенный AgentTask; проверяется фактическая модель снимка. Старые проверки required_features дополнены автоматически выведенными возможностями узлов.

- Ruff check/format: 62 файла; mypy Windows и `--platform linux`: 48 модулей, без ошибок. Реальный Linux runtime локально не запускался.
- OpenAPI/node schemas/TypeScript сгенерированы, `generate_contracts.py --check` прошёл. Frontend ESLint, Prettier, TypeScript/Vite build, 2 Vitest и 1 Playwright прошли.
- Wheel собран, установлен с 32 runtime-пакетами в отдельное окружение; миграция до 0006 выполнена. Для rpds-py в чистом Python-окружении потребовалась загрузка wheel из реестра; приложение не требует наличия uv-кэша.
- Проверены 69 локальных ссылок в затронутых документах; отсутствующих нет. Git diff --check прошёл. Удалённый CI и реальные model probes не запускались.

**Продолжение:** этап 4 на fake. Сохранить ограничения V-007–V-009, проверять совместимость snapshot, транзакционно применять переход/assignments и счётчики, не считать неизвестные capability разрешением на внешние действия. Реальных model probes и удалённого CI в ревью не выполнялось.

### 2026-09-14 · Этап 2A · Ревью, исправление выбора и снимков

**Статус:** замечания реализации исправлены, проверки прошли; этап 2A закрыт в пределах данных и конфигурационных API. Исходный отчёт сохранён следующей записью как история до ревью.

| Найденное несоответствие | Исправление |
| --- | --- |
| Два сбоя launcher/worker объявлены предсуществующими, но readiness сравнивал БД 0004 с ожидаемой 0003 | Ожидаемая ревизия обновлена до 0005. Оба исходных сбоя воспроизведены до исправления и прошли после него. |
| PATCH model_selections вызывал model_dump у уже сериализованного dict и возвращал 500 | Единая JSON-сериализация; смена direct/group и возврат к legacy direct атомарны. |
| Direct игнорировал собственный профиль/подключение; legacy role_assignments мог подавить выбранную группу | Выбор разрешается целиком и использует именно своего исполнителя; direct работает без дублирования legacy-настроек. |
| Не проверялись тип потребителя и архивность группы при создании binding; узловой выбор отсутствовал | Строгие варианты direct agent/direct llm/group, node.config.model_selection, проверки AgentTask/LLMRequest и новых привязок; одиночный узел использует тот же контракт. |
| Группа сохраняла ID кандидатов, но теряла настройки резервных профилей, endpoints, secret refs и итоговые параметры | Snapshot фиксирует всех кандидатов, даже отключённых, копии всех ресурсов и отдельные параметры каждого узла/кандидата. Архивный первый ресурс отмечается unavailable и не скрывает следующий. |
| Новые выборы и legacy overrides разных уровней могли действовать одновременно | Приоритет run → binding → version → defaults с полной заменой выбора; параметры роли разрешаются отдельно, списки заменяются. |
| Execution hash не менялся при изменении группы или её подключения | Run/resolve хешируют итоговую конфигурацию и зависимости; исходный hash версии хранится отдельно. Старые Run не переписываются. |
| Перестановка создавала новые ID, copy игнорировал expected_revision, неверный member-route мог дать 500 | Стабильные ID и ревизии кандидатов, проверка copy/archive, проверка типа до изменений, временное освобождение позиций внутри транзакции. |
| Триггер 0004 проверял только INSERT и только XOR ссылки | 0005 защищает INSERT/UPDATE: тип группы/ресурса, XOR, дубликаты пары, позицию и неизменность kind. История сохраняется. |
| Params могли сохранять plaintext credentials и переопределять разрешения/endpoint | Такие поля отклоняются без отражения значения в ответе; небезопасные старые params не выдаются и не экспортируются. Проверка поддержанных параметров конкретного адаптера остаётся этапом 3. |
| Перенос групп и автоматическое required_features отсутствовали при закрытом чекбоксе | Добавлены export/import определений, явная привязка каждого resource_ref, отказ конфликта имён; required_features пополняется при публикации и старте. Полный импорт графа остаётся этапом 3. |

**Контракты:** [MODEL_GROUPS](architecture/MODEL_GROUPS.md), [DOMAIN_DATA](architecture/DOMAIN_DATA.md), [runtime](architecture/RUNTIME_CONTRACTS.md), [состояния](architecture/STATE_MACHINES.md), [исполнение](architecture/EXECUTION_CONTRACTS.md), [безопасность](architecture/SECURITY_AND_OPERATIONS.md). Устранены противоречия про fallback, отдельную StepAttempt каждого retry, ограниченный новый проход после восстановления доступа, неизменность snapshot и сброс бюджетов. Генерируемые OpenAPI, node schemas и TypeScript обновлены.

**Проверки:** до исправлений исходный набор давал 92 passed / 2 failed. Итоговый Windows/Python 3.12.7 прогон: **127 passed**, 2 предупреждения Starlette/httpx и anyio, 64.66 секунды. Добавлены **33 регрессионных случая** в `test_stage2a_review.py`; исходный тест архивации уточнён (binding создаётся до архивации), проверка hash теперь сопоставляет resolve с фактическим Run. Новые сценарии охватывают наследование, снимки и секреты всех кандидатов, конкурентную перестановку, SQL-инварианты, миграцию 0004 → 0005, импорт/экспорт, перезапуск, legacy null и защиту старых params.

- Ruff check/format: 58 файлов без замечаний; mypy Windows и `--platform linux`: 43 исходных файла без ошибок. `generate_contracts.py --check` прошёл.
- Frontend ESLint, Prettier, TypeScript/Vite build, 2 Vitest и 1 Playwright прошли. Проверялся существующий интерфейс состояния служб/pairing; UI групп ещё не реализован.
- Wheel собран offline, установлен в отдельное окружение; его CLI успешно применил миграции к отдельной БД в `.local/review-2a-wheel-data`. Исходники проекта и рабочие каталоги пользователей для этого не использовались как data-dir.

**Границы и следующий шаг:** этап 3 — полная схема графа, capability/parameter validation, preflight, доверие и импорт pipeline. Исполнение кандидатов, retry/fallback, история переключений и session recovery — этапы 4–7. На этом проходе не выполнялись платные запросы или probe реальных моделей. Удалённый CI и запуск тестов на Linux не выполнялись.

### 2026-09-14 · Этап 2A · Группы моделей и типизированный выбор

**Статус исходного отчёта до ревью:** заявлено закрытие в пределах 2A. Утверждения о полноте snapshot, fully disabled, copy/revision и независимости двух сбоев уточнены и исправлены в записи ревью выше. Worker выбор и переключение кандидатов относятся к этапам 4–7.

**Реализовано.**

- Миграция `0004_model_groups` добавляет таблицы `model_groups` и `model_group_members` с триггером «ровно одно из profile/connection» и уникальным `(kind, name)`. Существующие `pipeline_bindings` получают столбец `model_selections_json` со значением `{}` по умолчанию; обратной миграции нет.
- ORM `ModelGroup`/`ModelGroupMember`, Pydantic-схемы `ModelSelection`, `ModelGroup`, `ModelGroupMember`, `ModelGroupAgentCreate/Update`, `ModelGroupLLMCreate/Update`, `ModelGroupCopy`, `ModelGroupMemberDelete`. `ModelSelection` нормализует выбор в `direct` или `group`: для `direct` требуются `model_id` и ровно один профиль/подключение, для `group` — только `group_id`. `SettingsOverrides` принимает обе формы (`model_selections` или legacy `model_overrides`); одновременное пересечение по одной роли отвергается.
- Сервис `agents_ide.services.groups` с create/list/get/update/archive/copy/replace_members/delete_member. Ревизия увеличивается на изменении метаданных и замене состава; archive без ревизии возвращает 409; пустой и полностью отключённый список отклоняется. `load_group_snapshot` отдаёт активную группу и упорядоченных кандидатов или `None`, если группа архивирована.
- REST: `GET/POST /api/model_groups[/agent|/llm]`, `GET/PATCH /api/model_groups/{id}[/agent|/llm]`, `PUT /api/model_groups/{id}[/agent|/llm]/members`, `DELETE /api/model_groups/{id}/members/{member_id}` с `expected_revision` в query, `POST .../archive`, `POST .../copy`. CSRF и pairing действуют.
- `services.settings.resolve_configuration` поднимает `model_selections` из колонки `model_selections_json` в конфигурацию с приоритетом run → binding → version; legacy `model_overrides` остаётся для прямого выбора. `capture_dependencies` записывает в snapshot полный список кандидатов группы (id, ревизия, member_index, enabled, profile/connection, model_id, params). Старт Run отклоняется с `model_group_unavailable`, если в snapshot нужна группа, но она архивирована.
- `WaitingReason` дополнен кодом `model_group_exhausted` (зарезервирован, выбор worker появится в этапе 4). В `schema_version` capabilities/features добавлен `model_groups`; в `schema/events` — `model_group.candidate_selected`, `model_group.candidate_skipped`, `model_group.candidate_switched`, `model_group.exhausted`. Сгенерированные OpenAPI/TypeScript-контракты синхронизированы.

**Нюансы и решения:**

- `model_overrides` сохранён как legacy-канал прямого выбора; новые записи должны использовать `model_selections` с `kind=direct`. На одно и то же имя роли оба поля одновременно недопустимы, иначе пересечение отвергается с 422. `binding_from_model` возвращает обе формы в ответе API, чтобы старые клиенты не ломались.
- Миграция `0004` в `upgrade()` создаёт только новые таблицы и столбец `model_selections_json`; триггерная проверка «один профиль или одно подключение» срабатывает раньше FK-проверок и преобразуется в `AppError("conflict", ...)` через `ensure_unique` (тест `test_group_member_mixed_kinds_rejected_by_schema` ожидает 422, потому что Pydantic отбивает лишнее поле раньше БД).
- Snapshot Run фиксирует полный упорядоченный список кандидатов группы в `dependencies["model_groups"][id]`. Редактирование состава или архивация группы после старта не меняют snapshot: тест `test_run_snapshot_pins_group_members_and_survives_edits` сравнивает snapshot до и после `PUT /agent/members`.
- Прямой выбор в `ModelSelection.kind="direct"` перекрывает `model_overrides[role]`, чтобы переход к новому контракту был постепенным, а существующие Run со старым `model_overrides` остались корректными.

**Проверки:**

- Backend на Windows/Python 3.12.7: 92 теста прошли (70 предыдущих + 22 новых из `tests/integration/test_stage2a_model_groups.py`). Два прежних сбоя `test_launcher::test_detached_launcher_sse_revoke_and_stop` и `test_storage_worker::test_health_separates_worker_readiness` относятся к независимой инфраструктуре worker/launcher и не связаны с этим этапом.
- Ruff check/format, mypy `Success: no issues found in 42 source files`. `scripts/generate_contracts.py --check` подтверждает синхронность OpenAPI/TS.
- Frontend: ESLint, Prettier, TypeScript/Vite build, Vitest и Playwright прошли без изменений (контракты генерируются, типов UI этап 2A не касается).

**Продолжение.** Этап 3 использует новые `model_selections` в валидации графа и preflight: agent-группа допустима только для `AgentTask`, llm-группа — для `LLMRequest`; несовместимые и отключённые позиции пропускаются с причиной. Этап 4 добавит серверный выбор кандидата по snapshot и события `model_group.candidate_*`/`model_group.exhausted`; `waiting_input(model_group_exhausted)` сохраняет прежние счётчики Run. UI выбора групп и диагностика переключения относятся к этапу 9.

### 2026-09-14 · Этап 3 · Описание графа и серверная валидация

Исторический отчёт до ревью: отметка полной готовности, семантика самосвязей и описание импорта ниже исправлены записью ревью и контрактом GRAPH_VALIDATION.

**Статус:** этап 3 закрыт. Полная схема узлов, AST с трёхзначной логикой, валидация графа (связность, лимиты, достижимость), preflight привязки и импорт/экспорт с пересчётом `execution_hash` реализованы и покрыты регрессионными тестами.

**Реализовано.**

- [`graph_schema.py`](../backend/src/agents_ide/domain/graph_schema.py) — JSON Schema для всех девяти типов узлов (`Start`, `End`, `AgentTask`, `LLMRequest`, `Command`, `CollectContext`, `GitCommit`, `Condition`, `PlanControl`); лимиты (200 узлов / 400 связей / 1 MiB / 64 KiB промпта / 50 подкоманд / 200 обратных переходов); `required_features_for` собирает требуемые движком фичи из состава графа.
- [`graph_ast.py`](../backend/src/agents_ide/domain/graph_ast.py) — `Op` (`const`, `ref`, `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `exists`, `all`, `any`, `not`), `ASTNode.from_json` с поддержкой сокращённого `{"const": value}` и `{"ref": path}`, трёхзначные `TruthValue` / `ValueType`, `EvaluationContext` (inputs / latest / work), `evaluate`, `evaluate_truth`, безопасная подстановка `{{ path }}` через `substitute`, валидация обязательных inputs.
- [`graph_validation.py`](../backend/src/agents_ide/domain/graph_validation.py) — `ValidationReport`, `validate_graph` (структура + лимиты + идентификаторы + связи + connectivity + Start/End + обратные связи), `validate_with_session` (дополнительно проверяет выбор модели, состояние групп и ресурсов), `preflight`, `import_payload` / `export_payload` (игнорируют `trusted` и `execution_hash`), `executable_payload` (без визуальных координат, входит в хеш версии).
- `api/domain.py` — эндпоинты `POST /graphs/validate`, `POST /graphs/import`, `POST /graphs/export`, `GET /versions/{id}/validate`, `POST /bindings/{id}/preflight`; старый ручной словарь `_NODE_SCHEMAS` заменён на источник из `graph_schema.node_schemas()`.
- `services/templates.py:create_version` строго валидирует граф (`allow_incomplete=False`) и пересчитывает `execution_hash` через `executable_payload`; `services/runs.py:start_run` дополнительно вызывает `preflight` перед записью снимка.
- Сгенерированные OpenAPI / node-schemas / TypeScript контракты обновлены; `generate_contracts.py --check` зелёный.

**Нюансы и решения:**

- Валидатор принимает `allow_incomplete=True` для черновика (`update_draft`) и `allow_incomplete=False` для `create_version` и эндпоинта `validate`. Это позволяет сохранять неполный граф, но блокирует запуск и публикацию до успешной проверки.
- Решение `decision` в `LatestResult` нормализовано в булевы значения (TRUE → `True`, FALSE → `False`, UNKNOWN → NULL) — это даёт честное трёхзначное поведение при сравнениях в AST.
- Импорт намеренно принимает и отбрасывает `trusted` и `execution_hash` (`extra="ignore"`), чтобы клиент не мог протащить доверие мимо сервера; `execution_hash` всегда пересчитывается через `executable_payload`.
- Самосвязи (edge `a→a`) выдают предупреждение `edge_self_loop` вместо ошибки: обратная связь всё ещё отслеживается лимитом `max_backward_transitions_per_run`.
- `validate_with_session` помечает недоступных/архивных кандидатов группы как `model_group_candidate_unavailable` (warning), а не как blocker, чтобы соответствовать правилу «недоступный первый кандидат не блокирует запуск при наличии следующего подходящего».

**Проверки:**

- Backend на Windows / Python 3.12.7: **142 passed** (127 предыдущих + 15 новых из `tests/integration/test_stage3_graph_validation.py`). Новые кейсы покрывают две обратные связи, отсутствующий Start/End, неверный ID узла, ссылку на несуществующий узел в Condition, цикл с выходом, недостижимый End, неподдерживаемые параметры модели, required_features, импорт с игнорированием `trusted`, экспорт с серверным hash, AST с трёхзначной логикой, preflight успешной привязки.
- Ruff check/format: без замечаний. mypy для Windows и `--platform linux` зелёные. `generate_contracts.py --check` подтверждает синхронность.
- Frontend: ESLint, Prettier, Vite build, Vitest, Playwright — без изменений (новые типы сгенерированы, UI не менялся).

**Границы и продолжение.** Полный разбор capability/формата ответа/бюджета, Git-план preflight и интеграционная проверка с реальными harness остаются этапам 6/7/8; импорт графа через UI относится к этапу 10. `preflight` пока не возвращает раздел `capability` для каждого кандидата — это требует capability-матриц адаптеров из этапа 0, которые будут добавлены после подключения первого harness. B-001/B-002/V-003 сохраняются.

### 2026-09-14 · Этап 9A · Планирование Council и подборка исходников

**Статус:** требования и справочные исходники подготовлены; функция не реализована.

В описание проекта и план добавлен Council: 2–3 независимых черновика, объединение, единый список вопросов, ответы и подтверждение ревизии перед фиксацией PlanItem. Работы выделены в этап 9A после внутреннего MVP, проверки — A71–A74. Другие предложенные продуктовые расширения Claudexor не включены.

В [sources](../sources/README.md) сохранены 39 неизменённых файлов Claudexor 3.11.0: Council, схемы/промпты/тесты, а также небольшие части для уже запланированных адаптеров и проверок. Приложены MIT License, manifest с размерами/SHA-256 и карта адаптации на Python. Исходный каталог без `.git`, поэтому commit SHA неизвестен. Подборка не подключена как зависимость и не является самостоятельной сборкой.

**Проверено:** все 39 копий совпадают с локальным источником и manifest по SHA-256; проверены размеры, комплектность и локальные ссылки обзора. **Границы:** upstream-тесты и реальные модели не запускались, исполняемый код Agents IDE не менялся. Следующий шаг этапа 9A — согласовать его серверные контракты и переносить отобранные алгоритмы с отличиями, перечисленными в обзоре sources.

### 2026-09-14 · Этап 2 · Ревью и исправление гарантий API/данных

**Статус:** замечания ревью исправлены; этап 2 закрыт с границами этапов 3–5/7. Предыдущая запись ниже — исходный отчёт до ревью; найденные расхождения уточняются этой записью.

**Что обнаружено и исправлено:**

| Проблема первоначальной реализации | Исправление и проверка |
| --- | --- |
| Доменный роутер обходил pairing и CSRF: анонимный GET projects возвращал 200 | Все маршруты подключены через существующую зависимость авторизации. Регрессия проверяет 401 без сессии и 403 при изменении без CSRF. |
| Четыре конкурентные команды с одной state_version принимались одновременно | Короткие `BEGIN IMMEDIATE` перед проверкой версии/sequence; commit/rollback завершается до HTTP-ответа. Один запрос проходит, остальные получают 409. Конкурентные старты с одним ключом создают ровно один Run/QueueJob/event; обновление черновика имеет одного победителя. |
| Идемпотентность пересчитывала snapshot из текущего binding и ломалась после его правки; chat/binding не полностью различались | Сохраняются `request_json` и hash нормализованного запроса; повтор проверяется до чтения изменившихся источников. Проверены повтор после правок и отказ при чужом chat.project_id. |
| Хеш команды не включал её тип, сам payload терялся | Хешируется весь нормализованный запрос; payload сохраняется отдельно. Проверены конфликт pause/cancel с одним ID и содержимое записи БД. |
| Снимок не фиксировал сообщения, настройки профилей, версию ключа и полный порядок overrides | Общий resolver defaults → version → binding → Run, затем параметры узла; снимок входов/зависимостей и реальные policy hashes. Проверены источники, параметры модели и сохранение старого DPAPI reference после ротации. |
| Резервации не исключали даже двух владельцев одного identity; не проверялись вложенные пути и общий checkout | Очередь отделена от владения каталогом. Worker-facing `reserve_workspace` атомарно проверяет identity, родителей и Git common directory; неизвестные старые scope не дают новый захват. Проверены одинаковый/вложенный каталог, общий и независимые checkout. |
| Проверка пути ограничивалась UNC-prefix, Git-ошибки могли выглядеть как чистый каталог | Проверяются абсолютный путь, доступность, локальный NTFS-том и актуальный identity. Git выполняется скрыто, без подменяющих GIT_* переменных; credentials удаляются из возвращаемого remote URL. Проверены кириллица, junction, подмена каталога и отказ DRIVE_REMOTE. Полный file-access guard остаётся V-003. |
| У шаблона фактически не было редактируемого графа-черновика, у чатов/сообщений отсутствовали операции изменения | Добавлены versioned draft/publish, чтение/изменение/архивация сообщений и изменение чата; ограничения архивации и неизменность input активного Run проверены. |
| Проверка провайдера записывала `ok` без HTTP-запроса | Возвращается 501 без изменения результата диагностики. Каталог выдаёт manual/cached модели, TTL и честный unverified/stale/fresh; URL/credential инвалидируют кэш. |
| TypeScript-генерация была отмечена выполненной, но отложена до этапа 9 | Добавлен детерминированный экспорт OpenAPI, node schemas и TypeScript; `--check` включён в CI и локальный check.ps1. |
| В ORM был CHECK по несуществующему role_dummy, а ошибки имён/уникальности давали 500 или детали SQL | Удалён фиктивный CHECK, ошибки нормализованы в 4xx без входных секретов/SQL. Добавлены ограничения неизменности snapshot/version, связи Run/chat и sequence в миграции. |

**Миграция и нюансы.** `0003_stage2_review` добавляется поверх 0002, сохраняя существующие строки и их снимки. Legacy-поля остаются неизвестными — V-006. Защита неизменности реализована и на уровне SQLite. Типы RunState, WaitingReason, ActiveInterval и место для capability AgentSession подготовлены; счётчики/переходы/применение RunPolicyRevision будут заполняться движком. Значения ключей не попадают в snapshot; хранятся только ссылки на версии SecretStore. Это уточнение синхронизировано с описанием проекта.

**Проверки:**

- Backend на Windows/Python 3.12.7: **72 теста прошли**, включая **35 новых регрессионных случаев** сверх исходных 37. Первый прогон регрессий воспроизвёл ошибки до исправлений.
- Проверено обновление 0002 → 0003 с сохранением Run/snapshot и `PRAGMA foreign_key_check` без ошибок. После ротации ключа отдельно перезапущен API: проект, версия, binding, подключение, профиль и Run совпали; старый SecretReference остался доступен. Проверены реальные DPAPI, junction и независимые SQLite-соединения; изменения Git выполнялись только во временных тестовых репозиториях.
- Ruff check/format, mypy для Windows и Linux target, синхронность генерируемых контрактов прошли. Linux target означает проверку типов; реальные Linux-тесты и удалённый GitHub Actions не запускались — V-002.
- Frontend: ESLint, Prettier, TypeScript/Vite build, **2 Vitest** и **1 Playwright** прошли. Chromium проверил pairing, API, reload и logout после подключения защиты роутера.
- Wheel собран offline, установлен с зависимостями в отдельное окружение и выполнил миграции. Остались два прежних предупреждения зависимостей TestClient; падений нет.

**Продолжение.** В актуальный план отдельно добавлен этап 2A с группами моделей: его требования не входят в проверенный исходный объём этапа 2. Далее этап 3 должен запретить dispatch до полной валидации графа, схемы/required_features и доверия hash; упрощённая публикация этапа 2 не означает готовность к исполнению. Этапы 4–5 подключают очередь, резервации, применение команд, интервалы времени и восстановление. Сохраняются B-001/B-002 и запрет старта с `allow_nonoverlap` до этапа 8. Все изменения находятся в рабочем дереве; коммит и удалённый CI в рамках ревью не выполнялись.

Ссылки: [доменный контракт](architecture/DOMAIN_DATA.md), [регрессии](../backend/tests/integration/test_stage2_review.py), [миграция](../backend/src/agents_ide/persistence/migrations/versions/0003_stage2_review.py), [генератор](../scripts/generate_contracts.py).

### 2026-09-14 · Этап 2 · Данные и конфигурационные API

**Статус:** завершён в пределах этапа 2.

**Реализовано.**
- Alembic-миграция `0002_domain` с таблицами `projects`, `chats`, `messages`, `pipeline_templates`, `pipeline_versions`, `pipeline_bindings`, `provider_connections`, `harness_profiles`, `runs`, `step_executions`, `step_attempts`, `agent_sessions`, `run_events`, `artifact_manifests`, `queue_jobs`, `command_journal`, `workspace_reservations`, `plan_items`, `run_policy_revisions`. Уникальные индексы, внешние ключи и CheckConstraints обеспечивают инварианты: одиночный `Start`/один `End` для шаблона не проверяется, но `revision`/`state_version`/`sequence` уникальны в пределах своего родителя.
- SQLAlchemy ORM в `persistence/models.py` использует декларативное объявление `Mapped[...]` со всеми связями; добавлены сервисные методы `mapping.py` для перевода ORM → Pydantic.
- Pydantic-схемы в `domain/schemas.py` задают API-контракт и валидацию: лимиты графа, regex для `base_url`, отказ `userinfo`, проверка протокола HTTP/HTTPS, типизированные роли сообщений и команды Run.
- Сервисы:
  - `services/projects.py` — создание с identity и Git-метаданными, оптимистическая версия, архивация блокируется активными Run.
  - `services/chats.py` — чаты и сообщения, заголовок уникален в пределах проекта.
  - `services/templates.py` — черновик (`pipeline_templates`) отдельно от неизменяемых `pipeline_versions`; `execution_hash` зависит от графа/фич/inputs, `policy_hash` фиксируется отдельно; `pipeline_bindings` хранит role_assignments/model_overrides/limit_overrides/branch_policy/dirty_policy.
  - `services/connections.py` — `provider_connections` с SecretStore, ротация ключей через новый `put` (старый ciphertext остаётся для in-flight Run), TTL каталога и тест-заглушка для этапа 7; URL проходит `validate_url` (HTTP только loopback).
  - `services/harness.py` — `harness_profiles` для codex/opencode.
  - `services/runs.py` — старт с `idempotency_key` + проверкой snapshot_hash; команды через `command_id` + `payload_hash` + `expected_state_version`; резервация рабочего каталога по `(identity_dev, identity_ino)`; таблица `plan_items` для пунктов плана; событие `run.created` фиксируется в той же транзакции.
- `services/runs.py` и `services/connections.py` используют `content_hash` для snapshot_hash и policy_hash.
- REST-роутер `api/domain.py` подключён к `app.include_router(domain_router)` и дополнен `/api/workspace/probe`, `/api/capabilities`, `/api/schema/nodes`, `/api/schema/events`.
- В `api/app.py` `lifespan` дополнительно создаёт `app.state.session_factory` (через `sessionmaker`) и `app.state.secrets` (SecretStore), чтобы зависимости `get_session`/`get_secret_store` могли отдавать сессию и хранилище секретов.
- Новый integration-тест `tests/integration/test_domain_stage2.py` (11 кейсов): создание/листинг/архивация проектов и чатов, проверка путей/identity, неизменяемость версии Run при правке привязки, идемпотентность старта, дубликаты command_id, secret round-trip без чтения, валидация URL, разрешённые настройки, эндпоинты схем.

**Нюансы и решения:**

| Наблюдение | Принятое решение / на что обратить внимание |
| --- | --- |
| ORM-класс `PipelineBinding` уже использовал `version` под столбец оптимистической версии | Колонка переименована в `revision` (и в миграции, и в ORM), а отношение к `PipelineVersion` — `pipeline_version`. В сервисах все обращения к `model.version` для `PipelineBinding` теперь читают `model.revision`. |
| `Bindin.revision` изначально конфликтовал с relationship-полем `version` | Найдено и устранено в ORM; следить, чтобы новые поля не пересекались с уже занятыми атрибутами. |
| Snapshot Run менялся при каждом вызове, ломая идемпотентность | В снимок не включаются изменчивые поля (`created_at`); добавлено явное `resolved_settings.message`, чтобы повтор с тем же ключом, но другим сообщением отклонялся как `idempotency_mismatch`. |
| `RunCommand` содержал клиентский `payload_hash`, что раскрывает контракт | Сервер сам считает `payload_hash` по нормализованному `payload` через `domain.common.payload_hash`. |
| Provider URL без userinfo отклоняется в схеме и в сервисе | HTTP допустим только для loopback; сохранено явное сообщение `connection_url_invalid`. |
| При удалении и замене секрета предыдущий ciphertext должен остаться для незавершённых Run | Ротация не удаляет прошлые файлы; окончательная очистка появится в GC этапа 12. |
| `workspace_reservations` хранит `identity_dev`/`identity_ino` без FK | Это намеренно: блокируем запись по каталогу, а не по проекту; владелец определяется `run_id`. |

**Границы фич.**
- Worker пока не подбирает задания из `queue_jobs`; реальный движок и события будут реализованы на этапах 4–5.
- `provider_connections.test` возвращает заглушку (`status=ok`, `models=[]`); реальный HTTP-probe появится в этапе 7.
- `ResolvedSettings` показывает источник binding/template, но `project_overrides` параметр зарезервирован, не используется UI.
- `node_schemas` содержит упрощённые схемы узлов; финальные JSON Schema и AST-валидация относятся к этапу 3.
- Endpoint `/api/runs/{id}/commands` фиксирует команду в журнале, но фактический переход состояния Run делает будущий движок.

**Проверки при завершении реализации.**
- Backend: **37 тестов прошли** (26 предыдущих + 11 stage 2). Новые сценарии покрывают идемпотентность, immutability snapshot, запрет разрушения активного Run, невозможность чтения секрета, дубликаты команд и валидацию URL.
- Ruff (`ruff check src/ tests/`) — без замечаний.
- mypy (`mypy src/`) — `Success: no issues found in 37 source files`.
- Wheel не пересобирался; миграция добавлена в пакет, alembic-head по-прежнему `0002_domain`.

**Продолжение.** На этапе 3 описать JSON Schema графа и серверную валидацию AST; узлы Start/AgentTask/LLMRequest/CollectContext/Command/Condition/GitCommit/PlanControl/End должны быть проверены, как и импорт/экспорт с доверием по `execution_hash`. Перед этапом 4 согласовать переход Run → running и реальный обработчик `queue_jobs`. B-001/B-002 остаются открытыми и блокируют автономные роли.

### 2026-09-14 · Этап 1 · Каркас приложения

**Статус:** завершён в пределах этапа 1.

**Реализовано.** FastAPI/Pydantic backend, SQLite/WAL и Alembic, отдельный worker с сохранённым heartbeat, локальный pairing, сессии и CSRF, DPAPI SecretStore, структурированные журналы и единый формат API-ошибок. Добавлены скрытый launcher `start/status/stop`, React/TypeScript/Vite-интерфейс входа и состояния служб, lockfiles, PowerShell-команды и Windows/Linux CI.

**Нюансы и исправления:**

| Наблюдение | Принятое решение / на что обратить внимание |
| --- | --- |
| При запуске из каталога Python-пакета локальный `logging.py` затенял стандартный модуль | Дочерние службы запускаются с рабочим каталогом данных. Не возвращать cwd внутрь `agents_ide`. |
| PID Windows venv-обёртки отличается от PID рабочего интерпретатора | Подтверждение старта привязано к случайному `launch_id`. Владение процессом проверяется по PID, времени создания ОС и executable. |
| Смерть venv-обёртки может оставить дочерний Python-процесс | Для API и worker используются отдельные Jobs. Перед перезапуском роли её прежний Job закрывается; другая роль продолжает работать. Процесс входит в Job до продолжения запуска. |
| Работающий HTTP API не означает готовность исполнителя | Health сообщает только доступность API; readiness учитывает БД и свежий сохранённый heartbeat worker. |
| DPAPI не работал под sandbox-пользователем без загруженного профиля Windows | Реальные DPAPI-проверки выполнены под пользователем запуска приложения. При недоступном контексте возвращается `secret_unavailable`, ciphertext сохраняется; plaintext fallback отсутствует. |
| Общий временный каталог pytest был недоступен в sandbox | Для проверки в этом окружении использовался отдельный `--basetemp` внутри игнорируемого `.local/`. Это особенность окружения разработки, не требование к установке приложения. |
| Ruff по-разному определял локальные импорты при запуске из корня и `backend` | В конфигурации явно заданы `known-first-party`; команды проверки согласованы с CI. |
| Миграции должны работать после установки wheel | Они включены в пакет в `src/agents_ide/persistence/migrations`. При установке wheel путь к отдельно собранному frontend задаётся через `AGENTS_IDE_FRONTEND_DIR`. |
| Каталог данных получает ограничивающий ACL | Нужен отдельный каталог на локальном NTFS. Нельзя использовать корень диска, каталог с посторонними файлами или путь через junction/symlink. |

**Границы фич.** Worker пока пишет heartbeat: очередь и выполнение pipeline ещё отсутствуют. `system/events` передаёт SSE-снимки состояния служб; это не durable-журнал Run с sequence/replay. React Flow установлен, но конструктор графа ещё не реализован. API проектов, чатов и подключений, ротация ссылок на секреты и снимки Run относятся к этапу 2 и далее. Автозапуск, backup/restore и восстановление активных Run ранним launcher не обеспечиваются.

**Проверки при завершении реализации:**

- Backend: **26 тестов прошли**, включая реальные DPAPI/ACL/Job Objects, stdio, junction, Git, pairing/CSRF, SSE, восстановление API после смерти процесса и независимость worker.
- Ruff и mypy прошли; отдельно выполнена проверка типов с Linux target. Реальный Linux-прогон тестов локально не выполнялся.
- Frontend: ESLint, Prettier, TypeScript/Vite build, **2 теста Vitest** и **1 сценарий Playwright** прошли. В Chromium проверены вход, перезагрузка страницы и отзыв сессии; внешний вид просмотрен на desktop/mobile.
- Wheel собран, установлен в отдельное чистое окружение и успешно выполнил миграции.
- Удалённый GitHub Actions не запускался — V-002. Остались предупреждения зависимостей TestClient о deprecated использовании `httpx` и alias `BlockingPortal`; на результат тестов они не повлияли.

Эти результаты перенесены из завершённого рабочего прохода этапов 0–1; при добавлении журнала тесты повторно не запускались.

**Продолжение.** На этапе 2 добавить доменные модели и конфигурационные API. Особенно учитывать неизменяемость снимков Run, версии ссылок SecretStore, identity/пересечение рабочих каталогов и запрет разрушения данных активного запуска. Полный перечень — в [плане](IMPLEMENTATION_PLAN.md).

Ссылки: [решения каркаса](architecture/FOUNDATION_DECISIONS.md), [эксплуатация](operations/FOUNDATION.md), [backend-тесты](../backend/tests), [frontend-тесты](../frontend/tests), [CI](../.github/workflows/ci.yml).

### 2026-09-14 · Этап 0 · Проверка контрактов и интеграций

**Статус:** проверка базовых возможностей и решение `go/no-go` завершены; B-001 и B-002 остаются открытыми для последующих этапов.

**Проверено.** На одноразовом Git-репозитории выполнены реальные запросы Codex 0.153.4 с `gpt-5.6-sol` и OpenCode 1.18.30 с `minimax-coding-plan/MiniMax-M3`. Подтверждены каталоги моделей, отдельные сессии, ответы, базовые события, прерывание, запрос разрешения с отказом и восстановление завершённых сессий после перезапуска. Для Codex также проверена ошибка при пустом отдельном `CODEX_HOME`; для OpenCode — 401 локального server без credentials.

**Нюансы и решения:**

- Первым harness для 6A выбран **OpenCode**, Codex оставлен для 6B. Это выбор порядка реализации адаптеров, не снятие ограничений автономной записи.
- Первый probe Codex interrupt завершился тайм-аутом. В control probe ожидали фактическую delta и сохраняли terminal-уведомление, которое могло прийти до ответа на управляющий запрос; получен подтверждённый `interrupted`. Первое наблюдение сохранено отдельно.
- Для сверки Codex использована схема установленной версии, для OpenCode — фактическая `/doc`. У OpenCode CLI default port=0 отличался от значения в документации; probe задаёт порт явно.
- На Windows для OpenCode используется реальный `.exe`. Ошибка PowerShell-обёртки или доступа к настройкам не считается отсутствием CLI либо недоступностью модели.
- Permission/reject проверяет отказ от действия. Проверка границ разрешённой записи требует отдельного сценария — B-001.
- Возврат ID и истории завершённой сессии после перезапуска не доказывает возможность безопасно повторить прерванную внешнюю операцию — B-002.
- Реальные model probes могут расходовать лимиты и запускаются явной диагностической командой. В fixtures сохраняются обезличенные метаданные, а не секреты и полный вывод модели.

**Продолжение.** Движок и пресет можно разрабатывать на fake по маршруту плана. Перед разрешением автономных ролей закрыть соответствующие ограничения из таблицы выше; обновлять capability только с новым наблюдением и fixture.

Ссылки: [матрица и команды повторения](integrations/CAPABILITIES.md), [smoke fixture](integrations/fixtures/2026-09-14-windows.json), [control fixture](integrations/fixtures/2026-09-14-controls.json), [негативные сценарии](integrations/fixtures/security-cases.json).

## Шаблон новой записи

```markdown
### YYYY-MM-DD · Этап N · Краткое название изменения

**Статус:** в работе / завершено / заблокировано.

**Реализовано:** фичи и изменения поведения, ссылки на код или документы.

**Нюансы и решения:** обнаруженная проблема, причина выбора решения,
временные обходы и условия их удаления.

**Блокеры и ограничения:** ID из таблицы, затронутая работа, условие снятия.
Если новых блокеров нет, указать это явно.

**Проверки:** что запускалось, результат, среда, пропуски и unverified.

**Продолжение:** конкретный следующий шаг и то, что нельзя упустить.
```
