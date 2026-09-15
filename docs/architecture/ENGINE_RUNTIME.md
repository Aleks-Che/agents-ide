# Движок графов после этапов 4, 5 и 7

Статус: независимый worker выполняет графы, поддерживает управление/recovery этапа 5
и [HTTP, Command, CollectContext этапа 7](LLM_COMMAND_EVIDENCE.md). Native harness,
GitCommit и PlanControl ожидают этапов 6/8. Основания: [план](../IMPLEMENTATION_PLAN.md),
[состояния](STATE_MACHINES.md), [runtime](RUNTIME_CONTRACTS.md), [графы](GRAPH_VALIDATION.md).

## Явное исполнение и границы

`POST /api/runs` принимает `execution_mode=real|simulated`, по умолчанию real.
В real работают Start/Condition/End, LLMRequest, Command и CollectContext. Не реализованные
AgentTask/GitCommit/PlanControl дают configuration_invalid с конкретной причиной ожидания.
В simulated поддержаны fake AgentTask/LLMRequest и Start/Condition/End; реальные процессы
и HTTP не подставляются в simulation. Подробности — в [контракте этапа 7](LLM_COMMAND_EVIDENCE.md).

В simulated-режиме можно передать `fake_scenario.responses`. Ключ ответа — node_id, необязательный visit_index и attempt_index (нумерация от 1). Ответ задаёт raw_text, verdict через JSON либо decision, outcome, retry_safety/no_effect, delay_seconds, deltas, files и optional телеметрию. Сценарий ограничен 200 ответами и 1 MiB; повторные ключи, некорректные пути и неизвестные исполнители отклоняются. Пример фрагмента:

```json
{
  "execution_mode": "simulated",
  "fake_scenario": {
    "responses": [
      {"node_id": "check", "visit_index": 1, "raw_text": "{\"verdict\":\"failed\",\"feedback\":\"исправить проверку\"}"},
      {"node_id": "check", "visit_index": 2, "raw_text": "{\"verdict\":\"passed\",\"feedback\":\"готово\"}"}
    ]
  }
}
```

При отсутствии ответа fake выдаёт явно simulated-результат. Сценарные записи выполняются только в отдельном `data_dir/simulated/<run_id>`; пользовательский workspace не изменяется. Относительные пути проверяются повторно, выход через drive/UNC/`..` и перенаправленный каталог запрещён. Fake не открывает сеть, не запускает Command/Git/harness. Timeout учитывается до сценарных записей. События и артефакты имеют source/source_kind=simulated, API Run — simulated=true.

Preflight принимает те же execution_mode/fake_scenario, что и Start; оба входят в итоговый execution_hash simulated-запуска. Для импортированной версии требуется доверие этому hash. Сценарий фиксируется в неизменяемом snapshot; после старта он не редактируется. Для simulated preflight обозначает локальное назначение данных и отдельный тестовый каталог, без отправки провайдеру. Реальные capability/permissions остаются unverified.

## Транзакции, очередь и checkpoint

Start фиксирует Run, snapshot, QueueJob и run.created одной транзакцией. Claim, продление lease, изменения состояния и последовательность событий используют короткий BEGIN IMMEDIATE и ORM commit с flush. Внешний вызов, Git-проба workspace и retry-ожидание находятся вне write-транзакции.

Claim атомарно фиксирует поколение и резервацию scope. Не более двух активных Run; пересекающиеся каталоги и общий Git checkout не исполняются одновременно. Занятый workspace пропускается при поиске другой работы. Worker поддерживает heartbeat отдельно от потоков Run и lease=30 секунд. Потеря продления запрещает новые действия; поколение, lease и резервация проверяются при каждой записи, срок — также перед commit. Истёкший либо отсутствующий владелец требует recovering: очередь не повторяет внешний вызов.

Каждое посещение, включая Start/Condition/End, создаёт StepExecution. Перед вызовом сохраняются StepAttempt, уникальный operation_id, кандидат, входной артефакт и dispatch intent. Итог попытки перечитывается и сохраняется в новой сессии БД. Подтверждённое завершение посещения, присваивания, следующий cursor и события фиксируются вместе. Для failed/unknown не возникает успешного latest.decision.

`Run.runtime_json` хранит work, следующий узел, cycle_id, loop_counts, visits, external_calls, длительность активного исполнения, retry_at, позицию/историю кандидатов и usage с quality. Активные интервалы хранятся отдельно. Пределы max_calls/max_node_visits/max_backward_transitions/max_duration_seconds и локальный loop.max_iterations действуют в runtime. Пропуск кандидата не расходует вызов; retry/fallback расходуют. При limit_exceeded новых попыток нет.

Результаты latest выбираются только из succeeded-посещений текущих cycle/scope. Condition маршрутизирует true/false/unknown по серверному boolean/null decision. Переход с loop увеличивает цикл и счётчики после вычисления assignments в старом контексте. Repair выбирает prompt_repair и сохранённый feedback. Невалидный JSON/output_schema/verdict даёт invalid_response_format; inconclusive идёт в unknown, а не false. Реальная актуальность evidence по версии файлов остаётся этапам 7–8.

## Кандидаты и ошибки

Список и настройки берутся из snapshot, изменяемая доступность профиля/подключения/секрета и directory identity проверяются перед dispatch. Архивные, отключённые, отсутствующие или изменившиеся ресурсы пропускаются с диагностикой. Каждая попытка сохраняет group ID/revision, member ID/index, модель, профиль/подключение, параметры и причину выбора. Новый visit начинает с первого приоритета.

Retry разрешён только для retryable_failure с `retry_safety=safe` **и** `no_effect=true`. Число повторов — node.max_retries (по умолчанию 2) на кандидата; retry_at с ограниченным backoff сохраняется до ожидания. После исчерпания этого retry либо подтверждённого unavailable с тем же доказательством допускается следующий кандидат. Operation ID одной попытки не переиспользуется у другого провайдера.

Unknown, transport_dropped, process_died и ошибка без доказательства отсутствия эффекта блокируют повтор и fallback. Permission и формат ответа имеют свои ожидания. Подтверждённая безопасная terminal failure завершает Run как failed; configuration_invalid требует исправления конфигурации. Бизнес-verdict failed остаётся успешно завершённым вызовом и направляет граф по false. Группа без кандидатов даёт model_group_exhausted с отдельным артефактом диагностики всех позиций; direct даёт model_unavailable.

Pause/stop/cancel обрабатываются на безопасных границах; приоритет cancel > stop > pause. Pause даёт завершиться текущему шагу. Stop/cancel перед следующим retry/fallback исключают новый вызов. Unknown нельзя превратить в подтверждённую отмену. [Этап 5](CONTROL_AND_RECOVERY.md) реализует resume/reconciliation, управляемую остановку и явный новый проход группы после восстановления доступа. Resume повторяет проверки blockers и продолжает прежнее посещение; неизвестный исход требует сверки. Checkpoint и бюджеты сохраняются; повтор execute не начинает Run заново. Резервация сохраняется для paused/stopped/waiting/recovering и снимается только при подтверждённом terminal-исходе.

## События, артефакты и наблюдение

Единый [каталог событий](../api/events.json) используется сервером и генерацией контрактов. Payload ограничен **16 KiB после JSON/UTF-8**; большой payload заменяется artifact_id. Sequence выделяется под той же write-транзакцией и уникален внутри Run. Delta сохраняются сразу через callback адаптера, без намеренной группировки/задержки; переходы и итоги также не буферизуются. Поздняя delta привязана к исходной попытке и помечается late.

Артефакты содержат реальные inputs, отрендеренный prompt, результат, assignments и причины переходов. JSON body сохраняется в БД, а не в динамическом ORM-атрибуте. Byte length/hash соответствуют очищенному содержимому. Выполняется рекурсивное удаление credential-полей и известных секретов, включая raw JSON/text; отмечаются redaction/truncation. Большие тела ограничены 10 MiB с сохранением корректного JSON.

API под прежними pairing/cookie/CSRF:

- `GET /api/runs/{id}/snapshot` — согласованные Run/state/runtime и last_sequence одной read-транзакции.
- `GET /api/runs/{id}/events?after=...&limit=...` — последовательная страница, has_more и reset_required.
- `GET /api/runs/{id}/events/replay` — первая сохранённая страница; дальнейшее чтение через cursor.
- `GET /api/runs/{id}/stream` — SSE с id/Last-Event-ID; заголовок имеет приоритет перед after.
- `GET /api/runs/{id}/artifacts[/{artifact_id}]` — список manifests и чтение очищенного тела.

SSE использует один poller на Run с подписчиками, короткие отдельные DB-сессии, backoff 200 мс–2 с и буфер клиента не более 1 MiB. История дочитывается полностью, в том числе после завершения Run; дубликаты удаляются по sequence. Недоступный cursor и переполнение требуют нового snapshot. Heartbeat — раз в 15 секунд, авторизация проверяется во время открытого соединения; отзыв сессии закрывает поток. После ухода последнего клиента poller удаляется. Полная retention/нагрузочная приёмка — этап 12.

## Совместимость

`0007_engine_review` добавляет runtime_json, selection_json, ссылки request/result-артефактов и body_json. Старые snapshot/hash не изменяются. Тела артефактов, утраченные первоначальным движком, восстановить из manifest нельзя; body остаётся null. Старое посещение без checkpoint не повторяется. Незавершённые записи без владельца направляются на recovering, неполный snapshot групп отклоняется. Миграции 0008/0009 и [правила этапа 5](CONTROL_AND_RECOVERY.md) добавляют сверку сохранённых результатов и владения процессами; legacy-записи без необходимых доказательств остаются заблокированы.
