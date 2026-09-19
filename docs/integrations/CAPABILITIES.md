# Проверка интеграций — этап 0

## Переключение провайдеров, 19.09.2026

Завершившаяся ошибка провайдера в OpenCode/Codex передаёт работу следующему
участнику группы независимо от HTTP-кода, имени native error и `isRetryable`.
Это включает HTTP 400 с `Connection prematurely closed BEFORE response`, ошибки
авторизации, квоты, обрыв транспорта и повреждённый ответ. Перед передачей runner
подтверждает остановку дерева процессов; файлы, последние действия и доступная
история сессии сохраняются. При неподтверждённой остановке новый агент не запускается.

В группах LLM завершившийся запрос с ошибкой также переключается вперёд, включая
HTTP 4xx/5xx, ошибку внутри HTTP 200, timeout и некорректный ответ. Возможные затраты
не объявляются нулевыми: разрешение переключиться сохраняется отдельно от `no_effect`.
Council использует тот же признак. Успешный ответ по-прежнему проверяется по схеме.
STOP/PAUSE и запрос разрешения на инструмент не вызывают переключение провайдера.
После перебора участников группа переходит в `model_group_exhausted`; повторного
обхода нет. Курсор следующего участника сохраняется вместе с результатом попытки.

Регрессии: `test_provider_fallback.py`, `test_stage6a_review.py`,
`test_stage6b_codex.py`, `test_stage7_real_llm_run.py`.

## Актуализация 17.09.2026

Нативный Council на Windows реализован с отдельными сессиями и одинаковым фиксированным
контекстом. Codex gpt-5.6-sol и OpenCode MiniMax-M3 вернули два принятых черновика;
локальный HTTP-merger создал итоговую ревизию. Полностью нативный merger пока не
проверен. Три разрешённых внешних вызова израсходованы, включая первый отказ Codex
invalid_json_schema; строгая схема исправлена перед успешным повтором.
[Отчёт](fixtures/2026-09-17-native-council.json).

Codex 0.153.4 command/exec с production private ACL подтвердил чтение разрешённого
контекста и отказ читать соседний synthetic .env или писать внутри/снаружи cwd.
Проверка не вызывает модель. Первый запуск требует однократного повтора безвредной
команды только при известной Windows 267. [Fixture](fixtures/2026-09-17-council-private-sandbox.json).
OpenCode использует --pure и запрет всех инструментов; автономная запись не разрешена.

Native provider 401 проверен через реальный OpenCode с локальным провайдером: отсутствие
вывода, пустые parts и нулевая usage дают безопасный provider_unauthorized; после
генерации ошибка не доказывает отсутствие эффектов. С 19.09 такой завершившийся
отказ разрешает передачу работы следующему агенту с сохранением изменений.
[Fixture](fixtures/2026-09-17-opencode-provider-auth.json).
Raw native envelopes теперь сохраняются после очистки. Per-model effort catalog
проверяется целиком; native auth/config fingerprint инвалидирует устаревшие сведения
и dispatch. Каталог не доказывает оплату, фактическую доступность модели или write capability.

B-001/B-002, полная capability-матрица и реальная матрица ошибок остаются открытыми.
Подробные результаты Windows/Linux/UI — в [журнале](../IMPLEMENTATION_LOG.md).
Прежние наблюдения ниже сохранены с их исходными границами.

Дата: 2026-09-14. Среда: Windows 10 22H2 x64, build 19045, Python 3.12.7, Node 22.20.0, npm 11.10.0, uv 0.9.15, Git 2.39.1.windows.1. Рабочая область — одноразовый локальный Git-репозиторий. Пользовательские проекты и настройки harness не изменялись.

Машиночитаемые наблюдения: [smoke](fixtures/2026-09-14-windows.json) и [permission/auth/recovery](fixtures/2026-09-14-controls.json). `supported` означает конкретное наблюдение указанной версии; `unverified` не означает отсутствия возможности. Synthetic fixtures не объявляются реальной интеграцией. Этап 0 завершён как проверка контрактов и решение go/no-go; неизвестные возможности не получают разрешение на исполнение.

| Возможность | Codex 0.153.4 | OpenCode 1.18.30 |
| --- | --- | --- |
| Транспорт | supported: stdio initialize/initialized | supported: HTTP, выделенный server с Basic auth |
| Каталог моделей | supported: model/list, 6 моделей | supported: /provider, выбранный ID присутствует |
| Выбранная модель | gpt-5.6-sol | minimax-coding-plan/MiniMax-M3 |
| Отдельная сессия | supported: thread/start | supported: POST /session |
| Реальный ответ модели | supported: turn/completed, без ошибки | supported: message, текст без ошибки |
| События | supported: lifecycle/item/delta | supported: SSE server.connected; полный поток model delta ещё не проверен |
| Продолжение по явному ID | supported: thread/resume, тот же ID и следующий turn | supported: после перезапуска server та же сессия и успешный второй ответ |
| Прерывание активной работы | supported в control probe: после первой delta interrupt → interrupted | supported: abort=true, затем idle |
| Ошибка авторизации | supported: отдельный пустой CODEX_HOME → auth_required | supported только для локального server: без credentials → 401; provider auth failure unverified |
| Permission/waiting_input | supported: commandExecution/requestApproval → decline, файл не создан | supported: bash permission → reject, ответ принят |
| Восстановление после закрытия транспорта и процесса | supported для завершённой сессии: ID и 1 turn восстановлены | supported для завершённой сессии: ID, 2 сообщения и новый ответ |
| Восстановление незавершённого внешнего действия | unverified | unverified |
| Изоляция агентной записи | unverified | unverified |

## Решение go/no-go

- **Этап 1: go.** Контракты зафиксированы, Windows-прототипы и приложение проходят локальные проверки.
- **Транспорт обоих адаптеров: go** для разработки интеграции. Положительный ответ модели проверен отдельно от CLI версии и наличия конфигурации.
- **Первый harness для 6A — OpenCode**, поскольку на этой машине подтверждены модель, HTTP/SSE, abort, permission/reject и продолжение после перезапуска, а server работает внутри проверенного Job. Codex выбран для 6B; его базовый протокол также прошёл проверки.
- **Автономные роли с записью: no-go для обоих**, пока не подтверждены границы разрешённых записей и восстановление незавершённого внешнего действия. Проверка отказа permission не доказывает изоляцию разрешённой записи. Эти capability gates должны быть закрыты до соответствующих ролей 6A/6B.

## Сверка протоколов

Для Codex извлечена JSON Schema именно установленной версии командой `codex app-server generate-json-schema --out <temp-dir>`. Проверены методы initialize, model/list, thread/start, thread/resume, turn/start и поля sandbox/approvalPolicy по [официальному App Server](https://learn.chatgpt.com/docs/app-server). Схема генерируется локально; большой полный bundle в репозиторий не включён.

Для OpenCode прочитана фактическая `/doc` OpenAPI запущенного server и сверены health, provider, session, message, event, abort с [OpenCode Server](https://opencode.ai/docs/server/). У установленной версии CLI default port=0, в документации приведён 4096: probe задаёт явный свободный порт, приложение не полагается на этот default. `--pure` отключает внешние плагины только в тестовом процессе.

## Отображение ошибок

Permission-запрос → `waiting_input(permission_required)` с external request ID и допустимыми действиями. Отсутствующая авторизация → `waiting_input(auth_required)`, потеря DPAPI → `secret_unavailable`. Разрыв после отправки без доказанного финала → `recovering`, затем при недостатке evidence `waiting_input(unknown_external_result)`. Повтор запроса автоматически не разрешается. Permission roundtrip подтверждён, но доменная машина ожидания реализуется в этапах 5/6. Восстановление завершённой истории не разрешает повтор незавершённой операции.

## Повторение probe

Команда делает реальные запросы к выбранным моделям и может расходовать их лимиты. Из корня репозитория после установки backend:

```powershell
backend/.venv/Scripts/python.exe scripts/probe_integrations.py `
  --opencode 'C:/path/to/opencode.exe' `
  --codex-model gpt-5.6-sol `
  --opencode-model minimax-coding-plan/MiniMax-M3 `
  --output docs/integrations/fixtures/new-observation.json
```

Нужен настоящий `opencode.exe`, а не PowerShell-обёртка. Существующий файл результата не перезаписывается. Output содержит только разрешённые метаданные; session IDs, пути пользователя, prompts, тексты ответов и credentials не сохраняются. Codex и OpenCode закрываются после probe. Ошибка/тайм-аут сохраняется как наблюдение, без автоматического повторного платного запроса.

Control probe повторяется командой `backend/.venv/Scripts/python.exe scripts/probe_controls.py --opencode 'C:/path/to/opencode.exe' --output <new-file.json>`. Он запрашивает разрешение только на синтетическое действие и всегда отказывает, проверяет завершённую историю после перезапуска, отдельно запускает Codex без сохранённой авторизации. Первый smoke получил тайм-аут Codex interrupt; после ожидания фактической delta и сохранения раннего terminal notification control probe подтвердил `interrupted`. Исходное наблюдение оставлено для воспроизводимости.

Оставшиеся gates 6A/6B: provider auth failure OpenCode, восстановление незавершённой операции, отрицательные проверки записи за пределами workspace. До их проверки соответствующие возможности остаются `unverified`; no-go автономной записи зафиксирован, а не заменён fake-проверкой.

## Ревью интеграции 6A — 2026-09-15

Установленный OpenCode 1.18.30 проверен через новый адаптер: session/message/SSE delta,
resume по native ID и directory с кириллицей. Модель заменена локальным HTTP-провайдером,
платных вызовов нет. [Наблюдение](fixtures/2026-09-15-stage6a.json) и
[границы реализации](../architecture/OPENCODE_RUNTIME.md). Предыдущий no-go разрешённой
автономной записи сохраняется; no_tools и успешный транспорт не закрывают этот gate.


## Ревью интеграции 6B — 2026-09-16

Codex App Server адаптер сверён с JSON Schema установленного **0.153.4**.
В отдельном временном CODEX_HOME и workspace реально проверены initialize,
пагинированный model/list (6 моделей), создание пустой read-only сессии и остановка
процесса. **Вызовов модели — 0**, пользовательский CODEX_HOME не подключался.
[Наблюдение](fixtures/2026-09-16-stage6b.json),
[контракт и ограничения](../architecture/CODEX_RUNTIME.md).

Strict fixture проверяет остальные transport/Runner сценарии: durable IDs,
разделение ролей, pause/resume, decline approvals, unknown после stop/обрыва и
cleanup принадлежащей группы. Эти проверки не подтверждают реальную модель,
изолированную запись или межharness fallback. Полная приёмка 6B остаётся открытой.

## Приёмка этапа 12 — 2026-09-16

Повторены реальные [smoke](fixtures/2026-09-16-stage12-smoke.json) и
[control probes](fixtures/2026-09-16-stage12-controls.json) на одноразовом Git-каталоге,
с `gpt-5.6-sol` и `minimax-coding-plan/MiniMax-M3`.
Оба harness вернули ответ модели. OpenCode подтвердил abort в smoke, permission/reject
и продолжение той же завершённой сессии после перезапуска в control probe.
Codex подтвердил read-only ответ, thread/resume, permission/decline без создания файла,
восстановление завершённой истории и interrupt после появления delta.

Быстрый Codex interrupt в smoke завершился `Empty`; исходная fixture сохранена.
Контрольный probe ожидал фактическую delta и получил `interrupted`. Отдельный Codex
auth probe снова завершился `Empty`: ошибка отсутствующей авторизации в этом проходе
остаётся unverified. Ошибка Basic auth локального OpenCode server не доказывает
отказ credentials его provider. Автономная запись, восстановление незавершённого
внешнего действия и реальный межharness fallback этими probes не подтверждены.
