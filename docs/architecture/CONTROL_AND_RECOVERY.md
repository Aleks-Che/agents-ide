# Управление, восстановление и диагностический CLI

Реализация этапа 5 после ревью. Основа: [машины состояний](STATE_MACHINES.md),
[контракты runtime](RUNTIME_CONTRACTS.md), [движок](ENGINE_RUNTIME.md).
Fake-сценарии проверяют серверную логику. Проверки Windows ProcessSupervisor используют
настоящие дочерние процессы. Native interrupt, permission и запрос внешнего статуса
Codex/OpenCode/HTTP относятся к этапам 6–7 и пока не объявляются поддержанными.

## Команды и продолжение

`POST /api/runs/{id}/commands` принимает `command_id`, `command_type`,
`expected_state_version`, `payload`. Совпадающий повтор возвращает сохранённый ответ;
изменённое содержимое с тем же ID или устаревшая версия дают 409. Принятие команды,
её применение и изменение состояния отражаются в журнале событий.

Приоритет неприменённых намерений: cancel > stop > pause. Pause позволяет текущему
вызову завершиться, сохраняет переход и останавливается перед следующим узлом.
Stop/cancel выставляют stop_requested. Fake проверяет сигнал остановки до файловых
эффектов; при частичном или неподтверждённом исходе остаётся unknown. Если успешный
ответ пересёкся со stop, результат и следующий cursor сохраняются: resume не повторяет
выполненное действие. Решение гонки с End определяется порядком транзакций.

Resume выполняется только явной командой. Runner сам не оживляет paused/stopped/waiting_input.
Сохраняются execution/visit/cycle, work, число попыток, расход вызовов, активная длительность,
история и позиция кандидата, его retry-бюджет и retry_at. Техническое продолжение создаёт
новую StepAttempt существующего StepExecution. Следующее посещение начинает выбор группы
с начала. Явное resume после model_group_exhausted допускает один новый проход snapshot;
накопленные счётчики и история остаются прежними.

Blockers не стираются командой pause/stop. Resume повторно проверяет остановку прежней
операции и причину ожидания. Resolve разрешён также в paused/stopped **при сохранённом
blocker**; состояние при этом не меняется. Это позволяет увеличить лимит после паузы,
не обходя проверку причины ожидания. Изменение snapshot, разрешений, графа или списка
кандидатов через resolve запрещено.

## Resolve

Увеличение лимита:

```json
{
  "command_id": "raise-calls-1",
  "command_type": "resolve",
  "expected_state_version": 12,
  "payload": {
    "limit_overrides": {"max_calls": 150},
    "reason": "Добавлены разрешённые попытки проверки"
  }
}
```

Разрешены только увеличение известных целых max_calls/max_node_visits/
max_backward_transitions/max_duration_seconds и причина limit_exceeded. Каждый изменённый
лимит получает RunPolicyRevision с предыдущим **эффективным** значением. Новая политика
применяется движком; счётчики не сбрасываются. Локальный loop.max_iterations остаётся
частью неизменяемого графа: для изменения его предела нужен новый Run.

`payload.data` сохраняется отдельным resolution_data-артефактом с command ID,
execution/attempt/cycle и удалением секретов. Ссылки передаются в контекст следующей
попытки; исходные input и критерии остаются прежними. Решение ограничено 1 MiB.

Неизвестный исход требует сверки. Простые data, pause, cancel или истечение lease
не доказывают остановку. Когда сервер подтвердил отсутствие живой прежней операции,
оператор может сохранить:

```json
{
  "reconciliation": {
    "attempt_id": "<unknown attempt ID>",
    "action": "retry_authorized",
    "evidence": "Что проверено и почему разрешена новая попытка"
  }
}
```

Исторический unknown сохраняется; такое решение разрешает новый вызов и не утверждает,
что прошлый вызов не имел эффекта. `action=accept_result` доступен только для сохранённого
позднего ответа, который сервер проверил по схеме. Тогда принимается фактический результат,
а не ответ из пользовательского payload. Нужна последующая команда resume. Сохранённое
намерение stop/cancel применяется прежде нового действия.

Для invalid_response_format/permission_required явный `payload.retry=true` разрешает
повтор в прежней конфигурации; новые права не выдаются. Неподтверждённый локальный процесс
блокирует и это действие. Native permission-ответы будут подключены в этапе 6.

## Владение, процессы и сбои

Lease и PID/create_time владельца хранятся в QueueJob. Истечение lease переводит Run
в recovering и делает задание доступным новому владельцу. **Резервация остаётся активной**:
claim атомарно переносит её на новое поколение, без окна для другого Run. Пока старый
владелец или его дерево живы либо их статус неизвестен, новый внешний вызов запрещён.
AccessDenied не считается доказательством выхода процесса.

ProcessSupervisor запускает процесс Windows приостановленным, включает в Job Object
с kill-on-job-close без breakaway и сохраняет PID/create_time/Run/generation/workspace
до ResumeThread. При ошибке регистрации процесс уничтожается до выполнения его кода.
Supervisor отслеживает потомков, включая переживших родителя.
В Windows venv зарегистрированный корень может быть процессом запуска Python;
фактический интерпретатор — его потомок и наследует Job Object.
Проверка занятого порта
не подключает приложение к чужому процессу. Поле transport/port — метаданные; проверка
готовности конкретного протокола будет реализована его адаптером.

На cooperative interrupt отводится 10 секунд, затем до 5 секунд на остановку локального
дерева. Истечение срока даёт process_not_responding и сохраняет резервацию. Поздний ответ
пишется отдельным артефактом/событием, не продвигает граф и не может записаться поверх
нового поколения владельца. Worker heartbeat, heartbeat попытки, process health и время
последнего внешнего события ведутся раздельно. Тишина вывода не означает зависание.

При первой обнаруженной ошибке проверки БД запрещаются новые dispatch и сигнализируется
остановка активной работы. После трёх последовательных неудачных проверок worker выходит.
Потеря продления lease также немедленно запрещает новые действия. Завершение worker
сначала сигнализирует остановку, затем ждёт потоки Run и закрывает принадлежащие ему Jobs.

Восстановление рассматривает журнал до любой новой попытки. Сохранённый успешный результат
применяется без повторного вызова; сохранённые ошибки формата/permission/terminal failure
не превращаются в retry. Незавершённый dispatch intent остаётся unknown, даже если PID уже
исчез. Позиция retry/fallback сохраняется атомарно с результатом. Проверяются directory/Git
identity, Git HEAD и отпечаток файлов private simulation. Изменение файлов между pause и
resume даёт external_change_detected. Полная проверка evidence, рабочего дерева реального
агента и Git intent относится к этапам 6–8.

## CLI и обслуживание

CLI работает через loopback API с cookie/CSRF. Он хранит сессию и точные запросы команд
в DPAPI SecretStore; файлы runtime содержат только ссылки. Для нового действия создаётся
новый command ID. Повтор с `--command-id` отправляет прежнее содержимое, включая ожидаемую
версию. Произвольный URL для отправки локального pairing-кода не поддерживается.
Ошибка API приводит к ненулевому exit code. Требуется запущенный API; worker нужен для
исполнения. Параметры `--data-dir`/`--port` указываются перед подкомандой.
Истёкшая CLI-сессия заменяется при следующем вызове; отозванная даёт auth_required.
Для явного повторного входа предусмотрен `--re-pair` у подкоманд CLI.

```powershell
python -m agents_ide projects list
python -m agents_ide projects create --payload-json project.json
python -m agents_ide chats create --project-id <id> --payload-json chat.json
python -m agents_ide runs start --payload-json run.json --command-id start-1
python -m agents_ide runs status --run-id <id>
python -m agents_ide runs events --run-id <id> --after 0 --limit 200 --follow
python -m agents_ide runs artifacts --run-id <id> --artifact-id <artifact-id>
python -m agents_ide runs diagnostics --run-id <id>
python -m agents_ide runs pause --run-id <id>
python -m agents_ide runs stop --run-id <id>
python -m agents_ide runs resume --run-id <id>
python -m agents_ide runs resolve --run-id <id> --payload-json resolution.json
python -m agents_ide runs cancel --run-id <id>
python -m agents_ide runs cleanup --run-id <id> --command-id cleanup-1
```

Events дочитывает страницы до актуального cursor; `--follow` ждёт новые события.
При reset_required следует прочитать новый snapshot. `runs diagnostics` показывает
resume_target/blockers/stop_goal, состояние попытки, health процессов и резервации.

`POST /api/runs/{id}/reservations/cleanup` принимает command_id/expected_state_version.
Он отклоняет активную очередь, живого или неизвестного владельца и неподтверждённое
дерево. Снятие резервации идемпотентно и записывается в command/event journal; состояние
Run и его результаты не переписываются. После снятия резервации resume снова проходит claim.

## Совместимость

`0008_stage5_controls` сохраняется в цепочке. `0009_stage5_review` добавляет PID/create_time
владельца и сведения о дереве/health/transport/workspace процесса. Снимки Run, исторические
результаты и hashes не мигрируются и не выдумываются. Записи без checkpoint или доказательства
владения деревом остаются на ручной сверке. Отсутствующий body артефакта нельзя восстановить
из hash. Создание новой миграции не делает старые неполные записи безопасными для replay.
