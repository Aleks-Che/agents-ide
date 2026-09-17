# Перенос сценариев Claudexor в Agents IDE

Дата: 17 сентября 2026. Основа — неизменённая выборка Claudexor 3.11.0,
39 файлов с размерами и SHA-256 в [manifest](claudexor-source-manifest.json).
Commit исходного локального каталога неизвестен. Исполняются Python/React-модули
Agents IDE; TypeScript runtime upstream не подключается и его Vitest не запускался.
MIT-уведомление включено в [серверный пакет](../../backend/src/agents_ide/THIRD_PARTY_NOTICES.md).

| Исходник в `claudexor/packages` | Адаптация | Проверки Agents IDE |
| --- | --- | --- |
| `orchestrator/src/council.ts`, `council.test.ts` | `services/planning.py`, `engine/planning_worker.py`: 2–3 слота, независимость фактических моделей, барьер перед одним merge, уменьшенный состав | `test_stage9a_dispatcher.py`: независимые входы, group snapshot/fallback, два из трёх, запрет считать один черновик Council; `test_stage9a_planning.py`: число слотов и повтор merger |
| `orchestrator/src/council-input.test.ts`, `council-input.integration.test.ts` | Только принятый, полностью проверенный JSON поступает в merge; ошибочный/противоречивый отчёт остаётся диагностикой. Бюджет, отмена и unknown принадлежат durable worker | `test_stage9a_dispatcher.py`: invalid/truncated success, budget before merge, cancel, restart, unknown без повторного обращения; `test_stage9a_retry_review.py`: сохранение черновиков и общего дедлайна |
| `orchestrator/src/planRun.ts`, `orchestrator/src/plan-prompt.ts`, `schema/src/plan.ts`, `orchestrator/src/planBrief.ts` | Pydantic `domain/planning*.py`, строгие JSON-промпты и серверные ревизии; пакет содержимого для HTTP, отдельные файлы для нативного merger | `test_stage9a_planning_run.py`: подтверждённая ревизия → immutable Run/PlanItem; `test_planning_native.py`: приватные каталоги попыток, чужие черновики только у merger |
| `orchestrator/src/planQuestions.test.ts` | single/multi/text, варианты, ответы, новая ревизия и confirmation hash. Терпимый Markdown-парсер не открывает подтверждение | `test_stage9a_planning.py`: все типы ответов, пустой список, неверный формат, stale revision, immutable confirmation; `test_stage9a_promote_review.py`: явное принятие единственного результата |
| `harness-codex/src/parse.ts`, `harness-opencode/src/parse.ts` и CLI fixtures | Сопоставление подходов к delta/final/tool/usage с App Server JSON-RPC и HTTP/SSE. CLI JSONL не используется как парсер другого транспорта. `adapters/native_events.py` сохраняет очищенные исходные envelopes, включая неизвестные события | `test_stage6a_review.py`, `test_stage6b_codex.py`, `test_stage6b_review.py`, `test_native_catalog_context.py`: scoped events, delta/final без дублирования, секреты, неизвестный envelope |
| `core/src/effort.ts`, `core/src/effort.test.ts`, `harness-codex/src/effort-probe.ts`, `harness-codex/src/effort.test.ts` | `adapters/model_catalog.py`: проверка всей live ladder, сохранение порядка, отказ при невалидном default. `services/harness.py`: TTL и fingerprint executable/native auth/config. Неподдержанное явно заданное значение отклоняется | `test_native_catalog_context.py`: malformed live ladder, порядок/default, отсутствие clamp, effort на проводе, внешняя смена auth и смена во время probe |

Точные пути всех выбранных файлов указаны в [manifest выбранных исходников](claudexor-source-manifest.json).
Имена строк таблицы описывают назначение модулей, а не обещают полный перенос
всех внутренних функций upstream.

Существенные отличия сохранены: member ID вместо одного файла на harness;
минимум два независимых принятых результата для Council; размер документа 64 КиБ;
одна попытка исправления формата за общий бюджет; никакого автоматического повтора
unknown. Вместо upstream clamp используется отказ; каталог без подтверждённых
метаданных не получает выдуманных возможностей. Авторизация берётся из нативного
хранилища, в БД фиксируется только отпечаток изменения.

Доступ к проекту реализован через фиксированный пакет выбранных файлов. Во время
его получения блокируются новые writers приложения и проверяются существующие
резервации; Windows handles не разрешают замену/запись до завершения копирования.
Участникам выдаются отдельные каталоги с одинаковым пакетом. Нативные вызовы и
проверки sandbox фиксируются отдельно от локальных тестов в журнале реализации.
