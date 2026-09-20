# Приёмка этапа 12

Актуальная проверка оставшихся пунктов: [19.09.2026](ACCEPTANCE_2026_09_19.md).
Подтверждены native structured pipeline, одиночный запуск и agent/LLM fallback
на локальном provider; все внешние условия полной v1 остаются явно перечисленными.
Ниже сохранены исходные результаты с датами их получения.

Дата: 16 сентября 2026. Эксплуатационная реализация и локальная проверка выполнены;
**полная v1 не объявлена принятой**. Прежние capability gates 6A/6B/9A сохраняются.
Рабочая среда проверки: Windows 10 Pro 10.0.19045, Python 3.12.7,
Node 22.20.0, локальный C: на Samsung SSD 990 PRO 4TB (NVMe).

## Покрытие матрицы A01–A74

Таблица связывает сценарии с воспроизводимыми наборами, а не заменяет их результат.
Fake/локальный HTTP/strict process fixture не подтверждают модель или изоляцию harness.
Все ссылки ниже относятся к проверкам в этом репозитории.

| Сценарии | Наборы | Граница |
| --- | --- | --- |
| A01–A03 | test_domain_stage2, test_stage2_review, test_stage9_messages, test_stage12_operations | Реальная SQLite/API, immutable snapshot |
| A04, A21 | test_stage6a_opencode, test_stage6b_codex, test_stage6b_review; real smoke | Модели проверяются отдельно, автономная запись открыта |
| A05 | test_stage7_real_llm_run, test_stage7_llm_commands_evidence, test_auth | Реальный локальный HTTP, DPAPI; внешний LLM provider отдельно |
| A06–A09 | test_stage4_engine_runner, test_stage8_git_plan, test_stage8_review | Fake + реальные команды/Git |
| A10–A13 | test_stage4_review, test_stage5_controls, test_stage5_review | Счётчики, pause/stop/cancel, поздний результат |
| A14–A16 | test_launcher, test_stage11_observation; observation.spec, pairing.spec | Независимый worker, несколько вкладок, реальный API/SSE |
| A17–A18 | test_stage4_review, test_stage5_review, test_git_paths | Lease/reconciliation, workspace identity |
| A19–A20 | test_stage8_git_plan, test_stage8_review | Реальный Git, intents, dirty/index/hooks |
| A22 | test_security, test_stage2_review, test_stage7_review | Пути/секреты, серверные ограничения |
| A23 | test_stage12_operations | Заполненная БД, update, backup/restore, hashes/DPAPI |
| A24–A27 | test_stage4_review, test_stage5_controls, test_stage5_review | Гонки, idempotency, durable worker |
| A28 | test_auth, test_stage11_observation, test_stage12_operations | Cookie expiry, retention floor, SSE reset |
| A29–A30 | recovery/test_processes, test_launcher, test_stage12_launcher | Windows Job Objects, чужой порт, процессы |
| A31–A32 | test_git_paths, test_stage2_review, test_security | Windows paths; неподдержанное отмечается отдельно |
| A33–A34 | test_stage7_llm_commands_evidence, test_stage7_review | Реальные argv/env/stdout, required/optional |
| A35–A37 | test_stage8_git_plan, test_stage8_review | Git baseline, hooks/signing, no_changes |
| A38–A39 | test_stage4_review, test_stage7_review | Общие лимиты, unknown telemetry |
| A40 | test_stage12_load; observation.spec | 100 000 событий, p95, пагинация, slow consumer, DOM limit |
| A41 | test_stage3_graph_validation, test_stage3_review; graph-editor.spec | Импорт и доверие hash, скрытые узлы |
| A42 | test_stage7_review, test_stage8_review, test_security | Evidence/AST/.env; изоляция записи реального harness остаётся открытой |
| A43–A44 | test_auth, test_security, test_stage7_review | Host/Origin/CSRF/pairing/redirect/URL |
| A45 | test_stage12_operations, test_security | Restore без DPAPI, diagnostics без secrets |
| A46–A47 | test_stage6a_review, test_stage6b_review, test_stage2a_review | Preflight/catalog/fallback; реальные capability gates сохраняются |
| A48–A50 | test_stage8_git_plan, test_stage8_review, test_stage7_review | PlanItem, no_progress, ограниченные evidence requests |
| A51–A52 | test_stage6a_review, test_stage6b_review; library-runs.spec | Раздельные роли/сессии, ограниченный контекст; fixture scope |
| A53 | test_stage5_controls, test_stage9_messages | Snapshot и resolution не меняют исходную задачу |
| A54–A55 | test_stage12_operations, test_stage9a_migration | Retention/backup/recovery, schema major/features, новая версия пресета |
| A56–A57 | test_stage3_review, test_stage7_review, test_stage8_review | Повторная проверка, AST unknown, stale evidence |
| A58 | test_launcher, test_stage12_launcher | Закрытие console, рестарт; reboot/чистая Windows остаются ручным gate |
| A59–A60 | test_stage7_review, test_stage5_review | HTTP unknown, lease/БД/поздний результат |
| A61–A62 | test_stage2a_model_groups, test_stage2a_review; settings/member-params.spec | API/UI групп и схемные параметры |
| A63–A67 | test_stage4_review, test_stage6a_review, test_stage6b_review, test_stage7_review | Fallback по snapshot; межharness с реальными моделями отдельно |
| A68–A70 | test_stage4_review, test_stage9_selection_summary; group-summary.spec | Группы при restart/repair/resume/retention |
| A71–A74 | test_stage9a_*, council.spec | LLM/agent direct/group, Windows native Council; полная нативная матрица ещё проверяется |

Backend-наборы находятся в [integration](../../backend/tests/integration),
[unit](../../backend/tests/unit), [recovery](../../backend/tests/recovery);
браузерные — в [e2e](../../frontend/tests/e2e).

## Измерения и результаты

Нагрузка: 100 000 подробных событий, входной вывод 12 000 000 байт с truncation,
полная пагинация без дубликатов/пропусков, 25 измерений каждого p95.
Серверная подписка — настоящий StreamHub с записью SQLite; браузерный замер
использует настоящий EventSource и видимый текст в React.

Первый замер backend: snapshot **13,66 мс**, первая страница истории **15,82 мс**,
фрагмент артефакта **5,63 мс**, persisted → subscription **82,55 мс**.
Буфер сервера ≤1 МиБ; медленный подписчик получает reset без влияния на быстрого.
Цели: snapshot <500 мс, история <1000 мс, persisted → UI <1000 мс.
Браузер: persisted → DOM **470,89 мс p95**, 25 измерений через MutationObserver,
12 строк в DOM при истории из 100 000 событий. [Исходные измерения](fixtures/2026-09-16-stage12-load.json).

Frontend: **147 Vitest**, **36 Playwright passed** (3,3 минуты), ESLint/Prettier,
TypeScript и Vite. Backend: **688 passed** за 19:22; последние регрессии WAL/GC
дополнительно проверены в целевых наборах (**44** и **30 passed**). Ruff, mypy
Windows/Linux и синхронизация контрактов прошли. Детали — в [журнале](../IMPLEMENTATION_LOG.md).
Архив с готовым UI проверен в новом runtime-only venv на этой же Windows: UI 200,
worker running, launcher stop/start, dev-зависимости отсутствуют. Первый кандидат
проверен с Python 3.13.10; в builder добавлен `.python-version` для выбора штатного 3.12.
Это не проверка отдельной чистой Windows и не физическая перезагрузка машины.

## Открытые условия выпуска

- Полная автономная запись и recovery незавершённых операций Codex/OpenCode,
  capability конкретных моделей и реальный межharness fallback: B-001/B-002.
- Полный Council с нативным merger и реальная матрица сбоев. Два нативных участника и фактическая read isolation уже проверены 17.09.2026; подробности ниже.
- Реальная перезагрузка Windows, установка на отдельной чистой Windows-машине
  и пользовательский Task Scheduler. Рестарт процессов и новый venv отмечаются отдельно.
- Удалённый CI должен пройти на поддержанных Windows/Linux, а пропущенные
  платформенные возможности остаются unverified.

До закрытия этих условий все A01–A74 не отмечаются как полностью пройденные.
Повторение: [README](../../README.md), [эксплуатация](OPERATIONS.md),
[реальные интеграции](../integrations/CAPABILITIES.md).


## Дополнение 17.09.2026

Реальный смешанный Council получил два принятых черновика от Codex gpt-5.6-sol и
OpenCode MiniMax-M3; локальный HTTP-merger создал готовую ревизию. Все owned processes
остановлены. Нативный sandbox Codex 0.153.4 проверен без модели: разрешённый файл
читается, соседний synthetic .env и запись внутри/снаружи каталога запрещены.
Первый запуск нового приватного cwd давал Windows 267; фиксированная безвредная
команда подготовки повторяется ровно один раз только для этой ошибки до обращения
к модели. [Итоговый probe](../integrations/fixtures/2026-09-17-council-private-sandbox.json).
8.3, SUBST, путь длиннее 260 символов и запрет замены удерживаемого каталога прошли
реальные Windows-тесты. Полная цепочка Git/harness под каждым алиасом остаётся V-003.

Первый общий прогон после изменений: Windows 708 passed / 4 failed; исправленные
Codex-сценарии и последние изменения проверены отдельно (51 passed). Linux/WSL
Python 3.12: 692 passed / 2 failed / 19 skipped. Исправлены переносимая фикстура
недоступного секрета и interval rollback при переводе часов назад. 35 браузерных
сценариев прошли; устаревший тест запрета всех agent Council заменён проверкой
атомарного серверного отказа при неверной конфигурации и прошёл отдельно.
Повторный общий Windows: 720 passed / 1 failed (устаревшее ожидание fingerprint);
после исправления целевой набор — 56 passed. Linux: 696 passed / 1 тот же failed /
24 Windows-only skipped; после исправления 132 passed / 1 Windows-only skipped.
Все новые Windows path tests прошли на Windows. UI member-params — 3 passed.
Полные и целевые прогоны учитываются раздельно в [журнале](../IMPLEMENTATION_LOG.md).

Новая поставка 17.09 проверена по 171 SHA-256, включая миграции 0018/0019 и MIT.
После offline установки в отдельный runtime-only venv Python 3.12.7 прошли migrate,
pairing, UI=200, worker=running и stop/start/stop. Данные и порт изолированы;
dev-зависимости не установлены. [Отчёт](fixtures/2026-09-17-release-runtime.json).
Это проверка текущего Windows-хоста; чистая ОС и физический reboot остаются открытыми.
