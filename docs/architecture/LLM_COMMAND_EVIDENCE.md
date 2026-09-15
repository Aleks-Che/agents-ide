# LLM, Command и CollectContext после ревью этапа 7

Дата: 15 сентября 2026 года. Реализация: [HTTP](../../backend/src/agents_ide/adapters/llm_http.py),
[команды](../../backend/src/agents_ide/engine/commands.py),
[контекст](../../backend/src/agents_ide/engine/context_sources.py),
[Runner](../../backend/src/agents_ide/engine/runner.py).
Это дополнение к [контрактам исполнения](EXECUTION_CONTRACTS.md) и
[управлению/recovery](CONTROL_AND_RECOVERY.md).

## Исполнение и HTTP

В `execution_mode=real` работают Start, Condition, End, LLMRequest, Command и CollectContext.
AgentTask, GitCommit и PlanControl возвращают `waiting_input(configuration_invalid)` с причиной
`harness_adapter_unimplemented`, `git_commit_unimplemented` или `plan_control_unimplemented`.
В simulated-режиме сохраняются прежние fake AgentTask/LLMRequest и отдельный тестовый workspace.
Реальные команды не включаются в simulation неявно.

URL проверяется до запроса: loopback HTTP или remote HTTPS, без userinfo/query/fragment.
HTTPX не наследует proxy из окружения и не следует redirect. Endpoint и версия ключа берутся
из snapshot конкретного кандидата. Ошибка расшифровки ключа не превращается в запрос без ключа.

HTTP работает в отменяемой async-задаче внутри синхронного адаптера. Учитываются общий deadline,
таймауты транспорта, stop token и владение lease. После отправки локальная отмена соединения
не доказывает отсутствие обработки на сервере: результат остаётся unknown.

| Исход | Политика |
| --- | --- |
| Отказ соединения, connect/pool timeout до отправки | Safe retry |
| HTTP 429 | Подтверждённый отказ, ограниченный retry; Retry-After задаёт минимальную задержку |
| HTTP 404, подтверждённая недоступность модели | Следующий кандидат из snapshot |
| HTTP 401/403 | Ожидание разрешения/авторизации, без перебора моделей |
| HTTP 408/409/425/5xx, read/write timeout после отправки | Unknown, без автоматического повтора/fallback |
| Ошибка внутри HTTP 200 или `choices[].error`/`finish_reason=error` | Unknown; частичный текст не становится успехом |
| Невалидный JSON, незавершённый SSE, превышение размера | Ошибка формата, без перебора моделей |

Ограничение ответа — 10 MiB, тела HTTP-ошибки — 64 KiB; ограничения действуют при чтении,
до JSON parsing. SSE выдаёт текстовые delta, проверяет JSON и завершающий `[DONE]`.
`Retry-After` поддерживает секунды и HTTP-date; jitter не уменьшает указанное ожидание.
Дедлайн retry сохраняется вместе с результатом попытки.

Параметры `stream`, `structured_output`, `timeout_seconds` проходят серверную валидацию.
`structured_output=true` запрашивает `json_object`; без него добавляется инструкция JSON.
В обоих случаях результат проверяется сервером. Эта настройка не доказывает capability
произвольной модели: реальные квоты, контекстное окно и поддержка параметров конкретным
провайдером остаются предметом явного probe. Автоматического понижения требований нет.

`POST /api/connections/{id}/test` ограниченно читает каталог и выполняет один минимальный chat.
Невалидный HTTP 200 не считается доступом. Отсутствие каталога допускается при выбранной вручную
модели. Результат диагностики не повышает конфигурационную ревизию и не инвалидирует активный Run;
при конкурентном изменении подключения устаревшая диагностика отклоняется.

## Команды и восстановление

Preflight проверяет команды после разрешения input-ref, env и cwd. Точные пути программ
сохраняются в `dependencies.command_programs` и входят в execution_hash. Worker использует
эти пути. Окружение состоит из минимальных переменных ОС и явного env; явные значения имеют
приоритет. Секреты в program/args/env запрещены.

Каждая подкоманда получает `command_ledger`-артефакт со статусом prepared до запуска и отдельный
артефакт с результатом после него. Результат включает вывод, код, required, retry_safety и
отпечаток workspace. Resume сохраняет тот же StepExecution. Подтверждённые completed/failed
unsafe-команды используются повторно как доказательства, без повторного запуска.
Prepared/unknown без подтверждённого исхода блокирует исполнение. Старый stage-7 Run без такого
журнала не получает выдуманную историю подкоманд.

На Windows процесс входит в Job и регистрируется в БД до ResumeThread. Компоненты cwd удерживаются
от замены во время CreateProcess. Вывод stdout/stderr делит один byte cap; сохраняется префикс,
остаток дренируется и учитывается в dropped_bytes. Проверяется всё дерево, включая потомка,
пережившего родителя. Stop запрещает следующую команду; pause завершает текущий список и
останавливается на границе узла. Сбой записи результата оставляет prepared/unknown для сверки.

Required failed/skipped/timeout исключает passed. Обрезанный required-вывод считается неполным.
Ошибка запуска — технический исход, не доказательство дефекта кода. Одна попытка списка Command
учитывается как один external call; пустой явный фильтр не запускает процессов и не расходует его.

## Контекст и актуальность

Поддержаны file/glob, diff от зафиксированного baseline, явный artifact и command_report.
Git-чтения выполняются через тот же supervisor, с ограничениями вывода/времени и отключёнными
external diff/textconv/fsmonitor. Защищённые пути исключаются и из diff. Файловый reader проверяет
именно открытый handle, размер и стабильность файла; перенаправленные/защищённые пути не читаются.

Лимиты 100 файлов / 256 KiB на файл / 2 MiB пакета / 64 KiB summary включают сериализованный JSON,
метаданные и все виды источников. Обрезка объектов не создаёт невалидный JSON. Manifest содержит
файлы/хэши, base/current HEAD, omissions и признаки редактирования секретов.

Дополнительно вычисляется консервативный отпечаток доступных файлов workspace: максимум 10 000
файлов и 64 MiB, без `.git`, служебных Python-кэшей и защищённых путей. Если доказать актуальность
не удалось, доказательства помечаются неполными. Это ограничение текущей реализации; оно не
заменяет Git baseline/allowlist и окончательный протокол evidence этапа 8.

LLM получает ограниченный пакет текущего scope. Успешный отчёт из другого цикла или от прежнего
содержимого файлов не подтверждает проверку. Исключённая фильтром required-проверка не исчезает
из агрегата. Изменение файлов во время ответа LLM или между кандидатами не становится passed.
Исходный текст модели хранится отдельно; серверный verdict становится failed/inconclusive,
если обязательные проверки упали или доказательства неполны. Отсутствующая usage ухудшает
достоверность бюджета и не подменяется нулевой стоимостью.

## Дополнение evidence

`resolve_requests` проверяет типизированные заявки против sources/exclude и исходных safe-команд.
Любая запрещённая заявка ведёт в missing_data. Он не исполняет команды. Результат подключается так:

```json
{"command_filter":{"ref":"steps.resolve.latest.validated_result.command_ids"}}
```

Ссылка должна указывать на CollectContext с mode=resolve_requests. Допускаются только заранее
заданные ID с retry_safety=safe; новые argv не принимаются. Пустой список пропускает исполнение.
Лимит — до двух возвратов на пару verifier/scope; значение 0 сохраняет смысл запрета.
Отсутствие нового evidence останавливает повторный сбор. Новые конструкции отмечены features
`command_filter_ref` и `llm_http_options`, старые snapshot не переписываются.

## Проверки и заимствования

Регрессии: [review](../../backend/tests/integration/test_stage7_review.py),
[HTTP/Command/CollectContext](../../backend/tests/integration/test_stage7_llm_commands_evidence.py),
[настоящий worker и локальный HTTP](../../backend/tests/integration/test_stage7_real_llm_run.py).
Они покрывают и полный Command → CollectContext → LLM с серверным отказом ложному passed.

Изучены и адаптированы сценарии [raw-api parsing](../../sources/claudexor/packages/harness-raw-api/src/parse.test.ts),
[terminal provider errors](../../sources/claudexor/packages/harness-raw-api/src/providerError.ts) и
[проверок](../../sources/claudexor/packages/review/src/gates.ts). Сохранены собственные
success_exit_codes, строгие типы, журнал и правила unknown; сторонний оркестратор не запускается.
Исходный снимок и [MIT License](../../sources/claudexor/LICENSE) сохранены без изменений.

Платные модели, удалённый CI, реальный Linux runtime и harness не проверялись в этом ревью.
Git commit/intent/hooks, PlanControl и полный пресет остаются этапу 8; harness — этапу 6.
