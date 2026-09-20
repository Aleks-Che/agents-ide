# Codex App Server: проверенная часть этапа 6B

Дополнение 19.09.2026: Runner передаёт `AgentTask.output_schema` в capabilities
адаптера, откуда она попадает в `turn/start.outputSchema`. Strict process fixture
проверяет это для direct/legacy выбора, продолжения и отдельной reviewer-сессии.
Повторён native command/exec probe запретов доступа без модели:
[результаты и границы](../operations/ACCEPTANCE_2026_09_19.md).

Ревью: 2026-09-16. Схема извлечена командой `codex app-server generate-json-schema`
из **codex-cli 0.153.4**; используемые request/approval схемы сохранены в
[fixture](../../backend/tests/fixtures/codex-0.153.4-requests.json), без описательного текста.
Сверка: [официальный App Server](https://learn.chatgpt.com/docs/app-server).

## Профиль и процесс

Абсолютный путь к native executable, `settings: {"permission_mode":"read_only"}`.
`approval_policy`, если задан, обязан быть `never`. Произвольные env/serve_args и
параметры моделей запрещены в preflight и dispatcher до подтверждения capability.
Режим выбирается в форме создания/редактирования harness-профиля.
Read-only не закрывает gate разрешённой автономной записи.

Run запускает один App Server через ProcessSupervisor/ProcessGroup с регистрацией
до возобновления процесса. Смена harness закрывает предыдущую группу в обоих
направлениях. Startup выполняет initialize ровно один раз и model/list с пагинацией
в том же соединении; дополнительные незарегистрированные app-server не создаются.
Все страницы входят в общий startup deadline и проверку владельца/stop.
Проверка версии не вызывает модель. Probe использует временный workspace, проверяет
реальный handshake/catalog; ошибка не выдаётся за успешный пустой каталог.

Окружение ограничено системными путями и native auth store, без provider keys/proxy
и произвольных overrides. Native auth/config читаются самим Codex. Приёмка влияния
native MCP/plugins/config на изоляцию остаётся открытой.

Stdout читается единственным reader: JSONL до 1 MiB на сообщение, очередь до 128
сообщений, до 10 MiB событий на turn. Невалидные/избыточные сообщения завершают
транспорт, а не пропадают бесследно. Stderr постоянно дренируется без сохранения.
Завершение закрывает Job/process group, дожидается процесса и закрывает pipes/readers.

## Протокол, сессии и исходы

Request IDs уникальны в пределах соединения и общие для всех экземпляров адаптера.
`turn/start` и `turn/interrupt` — запросы с ID; interrupt содержит threadId и turnId.
Ответы сервера отличают от входящих запросов даже при совпадении ID. Ранние события
сохраняются до ответа turn/start. Идентификаторы opaque, включая реальные UUID.

События принимаются только для текущих threadId/turnId. AgentMessage из item/completed
заменяет накопленные delta того же item, без дублирования; final_answer имеет приоритет
над commentary. Инструменты читаются из item/completed. Usage берётся из
thread/tokenUsage/updated → tokenUsage.last.totalTokens, без суммирования уже включённых
cache/reasoning. Стоимость неизвестна. Исходные envelopes, включая неизвестные типы, сохраняются как agent.native_event после удаления секретов; tool output и plan/diff дополнительно нормализуются. Большие Run payload перемещаются в артефакты и подчиняются квотам/retention.

Legacy direct-профиль из config.harness_profile_id также закрепляется в dependencies.
Runner сохраняет session_id и message_id до зависимых переходов, server_version и
permission_mode=read_only. Resume повторно задаёт cwd/model/read-only/never. Новая
сессия допустима при подтверждённом thread-not-found; временная ошибка resume не
маскируется созданием новой сессии. Активный незавершённый turn блокирует новый dispatch.
Роли изолированы, repair-продолжения ограничены общей политикой Runner.

Command/file approvals получают decline, permissions — пустой набор грантов,
user-input — пустые ответы, MCP elicitation — decline. Неизвестный server request
получает method-not-found. Эти ответы ничего не разрешают; запросы текущего turn
нормализуются в permission_required, интерактивное одобрение пока не реализовано.

Обрыв, provider failure и остановка после dispatch сохраняют unknown без no_effect.
На stop/deadline сначала отправляется native interrupt, затем завершается своя группа.
Остановка процесса не доказывает отсутствия внешних эффектов: автоматический retry
запрещён, требуется существующий reconcile/resolve workflow. Pause между шагами
сохраняет завершённую сессию и допускает её resume после запуска нового процесса.

## Проверенные границы

Реальный Codex 0.153.4: init, 6 моделей, пустой read-only thread и cleanup, изолированный
CODEX_HOME; **0 вызовов модели**. [Наблюдение](../integrations/fixtures/2026-09-16-stage6b.json).
Строгая фикстура: protocol, Runner, роли, IDs, approvals, long turn, stop/pause/resume,
ошибки startup/catalog/transport и ограничение output. Полный gate 6B с реальными моделями,
межharness fallback, подтверждённым model capability и write isolation остаётся открытым.


## Изолированный Council (17.09.2026)

Council использует отдельный именованный permission profile Codex 0.153.4: чтение
scratch-каталога и SYSTEMROOT, без сети инструментов. Legacy sandboxPolicy не
подменяет этот профиль при turn/start. Перед вызовом проверяются readiness Windows
sandbox и возвращённые activePermissionProfile/approvalPolicy/networkAccess. MCP
и дополнительные инструменты отключены; исходный проект не передаётся. Реальный
command/exec подтвердил чтение разрешённого файла и отказ читать соседний synthetic
.env и записывать новый файл. Этот профиль не разрешает автономную запись Run.
Подробности и ограничения — в [Council](PLANNING_COUNCIL.md).
