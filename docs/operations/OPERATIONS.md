# Эксплуатация Agents IDE

Команды выполняются из `backend` через `uv run --locked --no-dev agents-ide`.
Используйте один data_dir и один Windows-профиль для всех служб.

## Установка и авторизация harness

Проверенные версии этого прохода: Codex 0.153.4 и OpenCode 1.18.30. Установите
соответствующий CLI отдельной поставкой, поместите исполняемый файл в постоянный
локальный каталог и укажите его полный путь в **Настройки → Harness**.
Для OpenCode нужен настоящий `opencode.exe`, а не `.ps1`-обёртка. При установке
через npm он находится внутри пакета `opencode-ai/bin` либо его платформенного пакета.
Версию сверяйте самим выбранным executable, а не другой командой из PATH.

В обычном пользовательском терминале выполните `codex login` и/или
`opencode auth login`. Это интерактивный вход самого CLI: backend не хранит его
пароль. После входа запустите проверку профиля в UI, обновите каталог, выберите
точный model ID. Настройте режим `no_tools` для OpenCode или подтверждённый
`read_only` Codex; не включайте автономную запись по одному успешному login.
Ключ обычного LLM-подключения задаётся отдельно в **Настройки → Подключения**.
Данные авторизации harness не входят в backup Agents IDE.

Связь executable/version/capability и команды повторения probes:
[CAPABILITIES](../integrations/CAPABILITIES.md). Смена версии требует новой проверки;
catalog success не доказывает доступ к выбранной модели или изоляцию инструментов.

## Службы и безопасная граница

`start` запускает скрытый launcher/API/worker. Закрытие терминала не завершает службы.
`status` различает stopped/starting/running/recovering/stale/failed. Health API не
подтверждает готовность worker: readiness учитывает БД и heartbeat. Launcher проверяет
PID, время создания и executable, ограничивает перезапуски с backoff.

Backup/update/gc/compact/restore создают durable maintenance gate: API отклоняет
мутации с `maintenance_in_progress`, worker не берёт новые задания. Run доходит
до границы шага и становится paused. Выполняющийся Council завершается либо достигает
своего дедлайна. Обслуживание ждёт до 60 секунд; таймаут не разрешает копирование
живых данных. Затем launcher останавливается и захватываются locks writer-служб.
После успеха службы запускаются снова; paused Run требует явного resume.
Ранее ожидавшие queued Run остаются в очереди.

Самостоятельные `api`/`worker` сначала остановите через Ctrl+C: занятый lock
отклоняет обслуживание. Stale heartbeat не заменяет lock. Фоновый GC запускается
при свободных dispatch-пулах, примерно раз в минуту, только для терминальных Run.

`stop` службы прерывает локальные операции и завершает принадлежащие процессы.
Если важна граница шага, сначала нажмите **Пауза** и дождитесь paused.
Pause заканчивает текущий шаг; Stop прерывает его; Cancel завершает Run.
После выключения компьютера отсутствие процесса не доказывает отсутствие эффекта:
worker сверяет владельца, attempts, процессы и Git intents. Известный checkpoint
может продолжиться, unknown требует сверки. При restore автоматически выполняется
только reconciliation, затем paused/waiting_input без внешнего вызова.

## Backup и restore

`backup C:/Backups/agents-001` требует новый каталог вне data_dir и устанавливает
приватный ACL. `--include-secrets` опционально добавляет только DPAPI ciphertext.
`verify-backup C:/Backups/agents-001` проверяет готовую копию.
Restore: `--data-dir C:/AgentsIDE-restored restore C:/Backups/agents-001`.

SQLite копируется через backup API; проверяются integrity/FK и hashes артефактов.
Внешние артефакты допускаются через проверенный `artifacts/` source_ref.
`files_json` описывает evidence в рабочем репозитории: эти файлы не копируются
и не удаляются. Manifest с форматом, версиями, безопасной конфигурацией, размерами
и SHA-256 записывается последним. Каталог без manifest — незавершённый backup.
SHA-256 выявляет повреждение, но не удостоверяет автора: используйте доверенную копию.

Restore проверяет пути, ссылки, версии, hashes/FK до публикации БД и повторно
проверяет staged-копию после копирования. Непустые db/artifacts/secrets запрещены.
Sessions/pairing/heartbeat сбрасываются, leases аннулируются, generation увеличивается.
Подтверждённые Council и история сохраняются; незаконченные Council-вызовы становятся
unknown, задания — failed для явного retry. Run snapshot не переписывается;
незавершённые Run переходят в recovering, затем paused/waiting_input.

Репозитории, Git hooks и авторизация самого harness не входят в backup. Перед resume
сверьте checkout/ветку, изменения и незавершённые действия. Для unknown используйте
Resolve с evidence: принять сохранённый результат либо явно разрешить повтор,
который может повторить платный вызов или побочный эффект.

На другом Windows-профиле `secret_unavailable` не удаляет ciphertext и не включает
plaintext fallback. Повторно задайте ключ в настройках; для закреплённого Council
используйте явную ротацию доступа без смены endpoint. Без `--include-secrets` ключи
вводятся заново. После прерванного restore используйте новый пустой data_dir.
Сохранённые в manifest настройки предназначены для сверки: env/порт/квоты
задаются при запуске восстановленных служб, автоматически в окружение не записываются.

## Обновление и поставка

Перед заменой кода сделайте backup старой версией и остановите службы. Разверните
новую поставку рядом, сохраните data_dir и установите зависимости. Выполните
`update --check`, затем `update --backup-dir C:/Backups/pre-update-003`,
`diagnostics` и `start`.

`update` обновляет БД/presets для установленного кода, не скачивает приложение.
Проверяет ревизию БД, schema/features версий и совместимость нетерминальных snapshot.
Backup создаётся перед миграцией. Изменённый preset добавляется новой immutable-версией;
пользовательские шаблоны, копии, bindings и Run snapshots сохраняются.
Несовместимый нетерминальный Run завершите прежней версией приложения.

Ошибка после начала записи update/restore удерживает gate и оставляет службы
остановленными. Откат — совместимый код и restore backup в новый каталог;
произвольный downgrade не обещается. После диагностики `maintenance-clear` снимает
gate только при свободных locks launcher/API/worker/migrate/operations.

`scripts/build_release.py --output <новый.zip>` собирает UI с lockfile, упаковывает
Python sources/uv.lock, готовый UI, документацию, install/run scripts и SHA-256 manifest.
Node нужен для сборки. Установка Python-зависимостей требует источника/кеша uv;
архив не обещает полностью автономную установку без интернета. Wheel собирается
`uv build`; для него задайте `AGENTS_IDE_FRONTEND_DIR` на готовый UI.

Task Scheduler пользователь настраивает отдельно: вход текущего пользователя,
загруженный Windows-профиль, скрытый `agents-ide start`, тот же data_dir.
LocalSystem и работа после выхода пользователя не поддержаны. Приложение
не создаёт задание и не меняет политику Windows автоматически.

## Квоты и очистка

| Объект | Правило по умолчанию |
| --- | --- |
| Event | Payload до 16 КиБ; больший — артефакт |
| Артефакт | До 10 МиБ JSON; превышение — truncated envelope |
| Команда | stdout/stderr до 10 МиБ; дальнейший вывод дренируется |
| Run | 1 ГиБ артефактов и 100 000 подробных событий |
| data_dir | 10 ГиБ; запас 64 МиБ для остановки/критичных записей |
| История | 30 дней после завершения; active и pinned защищены |

Квота проверяется перед новым Run/Council, шагами и записью артефактов. Превышение
на границе вызывает `waiting_input(limit_exceeded)`. Если результат внешнего вызова
не удалось записать, durable intent остаётся для recovery. Отказ БД прекращает
новые dispatch. Критичные события не отбрасываются при лимите delta.

`diagnostics` показывает размер и остаток диска. Увеличьте лимиты env, перезапустите
службы и выполните resume: прежний расход не обнуляется. Размер включает WAL,
logs/secrets/temp/simulated. После GC файл SQLite может не уменьшиться;
`compact` выполняет checkpoint/VACUUM под locks остановленных writer-служб.

`pin RUN_ID` защищает Run; `pin RUN_ID --remove` снимает защиту. `gc` порциями удаляет
подробные delta терминальных незакреплённых Run и неиспользуемые `event_payload` bodies.
Результаты/evidence/Git intents/PlanItem/команды/manifests сохраняются. Неизвестные
типы и файловые source_ref сохраняются консервативно. Ссылки проверяются по всем
Run/PlanItem/attempts/событиям/артефактам, а не только по владельцу.
Курсоры по кругу обходят до 50 Run и 100 payload за один проход: большое число
защищённых ссылок не оставляет последующие записи без проверки. Курсоры — только
подсказка порядка; решение об удалении всегда принимается по текущему состоянию БД.

Две фазы: tombstone с commit → повторная проверка ссылок/pinned/state → удаление body
с `purged_at`. Сбой между фазами возобновляем. Запрос удалённого тела возвращает
`artifact_expired`/410. Retention floor заставляет старую SSE-подписку восстановить
snapshot; критичная история до floor доступна через пагинацию.

## Диагностика

`diagnostics` выводит metadata JSON: версию/schema, UI, порт, диск/квоты, heartbeat
worker/attempts, leases/generation, последнее внешнее событие, доступность DPAPI,
время каталогов harness/LLM и наличие Git. Секреты, prompts/source/argv и полные
журналы не экспортируются. Сетевая проверка credentials — отдельная кнопка проверки
подключения; доступный DPAPI не доказывает принятие ключа provider.

| Симптом | Действие |
| --- | --- |
| Worker unavailable/stale | status/diagnostics, logs/worker.jsonl, перезапуск |
| `port_unavailable` | Другой --port для start/auth; чужой сервис не завершайте |
| `frontend_unavailable` | Release с UI либо сборка frontend |
| `secret_unavailable` | Тот же Windows-профиль либо повторный ввод ключа |
| `model_group_exhausted` | Причины кандидатов → восстановление доступа → resume |
| `limit_exceeded` | Диск/размеры → GC/compact либо увеличение квоты |
| `unknown_external_result` | Checkout/процессы/результаты → Resolve перед resume |
| `schema_unsupported` | Совместимый runtime/backup, завершение старого Run |
| Maintenance после сбоя | Проверка backup/состояния записи, затем maintenance-clear |

Корреляция событий: run/execution/attempt/session/generation/command ID.
Host/Origin/CSRF защищают API/SSE. [Приёмка](RELEASE_ACCEPTANCE.md) отделяет
реальные платформенные проверки от fixtures и открытых gates.
