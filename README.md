# Agents IDE

Локальная рабочая область для проектов, чатов, групп моделей и визуальных pipeline.
React-интерфейс обслуживается FastAPI; отдельный Python worker сохраняет работу
в SQLite/WAL и продолжает её без браузера.

Реализованы конструктор, direct/group, preset с PlanItem/Git, Council для LLM,
наблюдение через SSE и эксплуатационные команды этапа 12. **Полная v1 ещё не
принята:** автономная запись реальных harness, часть Codex/OpenCode и Council через
harness остаются за capability gates. См. [план](docs/IMPLEMENTATION_PLAN.md),
[приёмку](docs/operations/RELEASE_ACCEPTANCE.md) и [интеграции](docs/integrations/CAPABILITIES.md).

## Установка

Основная среда: Windows x64, локальный NTFS, обычный пользователь с загруженным
Windows-профилем. Проверенная машина: Windows 10 22H2 build 19045. Нужны Python
3.12–3.13, uv (проверен 0.9.15) и Git (проверен 2.39.1.windows.1).
Linux используется для разработки; DPAPI там возвращает `secret_unavailable`.

Архив поставки содержит готовый `frontend/dist`, Python sources, lockfile
и скрипты. Node для запуска такой поставки не нужен. Распакуйте архив и выполните:

```powershell
./scripts/install.ps1
./scripts/agents-ide.ps1 start
./scripts/agents-ide.ps1 auth pair-code
```

`install.ps1` предназначен для новой установки. Для существующих данных используйте
процедуру update с backup из [эксплуатационной инструкции](docs/operations/OPERATIONS.md).

Если выполнение .ps1 запрещено политикой Windows, выполните команды напрямую
из `backend`, не меняя глобальную ExecutionPolicy:

```powershell
uv sync --locked --no-dev
uv run --locked --no-dev agents-ide migrate
uv run --locked --no-dev agents-ide start
uv run --locked --no-dev agents-ide auth pair-code
```

Откройте **http://127.0.0.1:8765**. Код одноразовый, действует пять минут; сессия —
12 часов. Новый код: `auth pair-code --rotate`. Cookie/CSRF/код не передаются в URL.

Для установки из checkout сначала соберите UI (Node 22.12+ в ветке 22, npm 10–11;
проверены Node 22.20.0 и npm 11.10.0):

```powershell
cd frontend
npm.cmd ci
npm.cmd run build
cd ../backend
uv sync --locked
uv run --locked agents-ide start
uv run --locked agents-ide auth pair-code
```

`start` запускает скрытые launcher/API/worker под текущим пользователем.
Закрытие терминала не завершает их. `status` показывает состояние; `stop`
останавливает принадлежащие приложению процессы. Занятый порт не освобождается
принудительно. Работа после выхода из Windows не обещается. Автозапуск не устанавливается.

## Первый проект и запуск

1. В настройках добавьте LLM-подключение: URL, ключ, модель. Если каталог недоступен,
   введите ID вручную. Ключ нельзя прочитать обратно. Harness устанавливается
   и авторизуется отдельно под тем же пользователем; укажите реальный executable
   и выполните проверку подключения. Поддержанные версии: [интеграции](docs/integrations/CAPABILITIES.md).
2. Создайте проект с локальной папкой вне data_dir Agents IDE, затем чат.
3. В **Настройки → Группы моделей** создайте группы типа `agent` или `llm`.
   Имена `heavy` и `flash` произвольны: группа содержит упорядоченные модели
   с конкретным профилем/подключением. Первая подходящая позиция имеет высший
   приоритет. Настройте параметры и отключите ненужные позиции.
4. В **Библиотеке** скопируйте пресет «Разработка с планом» либо создайте граф.
   Опубликуйте версию и binding для проекта; выберите direct/group для ролей,
   команды проверок и разрешения. Пользовательская копия независима от обновлений пресета.
5. В чате нажмите **Запустить**, задайте план вручную или выберите подтверждённый
   Council. Проверьте preflight: модели, команды, пути и права. Импорт требует
   доверия execution hash; неподтверждённые capability блокируют запуск.
6. Экран Run показывает граф, цикл, PlanItem, попытки, фактическую модель, fallback,
   результаты и историю. Закрытый браузер не влияет на worker.

Полный preset с автономной записью ограничен gates реальных harness. Для реального
сценария используйте разрешённые LLM/Command/CollectContext или подтверждённый режим
harness без инструментов. Simulated Run — явный тестовый режим.

При `model_group_exhausted` откройте причины кандидатов, восстановите доступ
и явно продолжите Run. Изменение группы не меняет текущий snapshot. Timeout после
отправки, невалидный JSON, failed-вердикт и запрос разрешения не разрешают
бесконтрольный fallback.

## Обслуживание

Из `backend`:

```powershell
uv run --locked --no-dev agents-ide diagnostics
uv run --locked --no-dev agents-ide backup C:/Backups/agents-001
uv run --locked --no-dev agents-ide verify-backup C:/Backups/agents-001
uv run --locked --no-dev agents-ide update --check
uv run --locked --no-dev agents-ide update --backup-dir C:/Backups/update-001
uv run --locked --no-dev agents-ide --data-dir C:/AgentsIDE-restored restore C:/Backups/agents-001
uv run --locked --no-dev agents-ide pin RUN_ID
uv run --locked --no-dev agents-ide gc
uv run --locked --no-dev agents-ide compact
```

Backup требует новый каталог вне data_dir. `--include-secrets` добавляет DPAPI
ciphertext, привязанный к Windows-профилю. Репозитории не копируются. Restore требует
новый data_dir и сверку checkout перед продолжением. Копирование одного live .db
не заменяет backup. После ошибки записи update/restore gate запрещает запуск
до завершения восстановления; `maintenance-clear` работает только при свободных locks.

Подробно: [stop/resume, backup/update, квоты и диагностика](docs/operations/OPERATIONS.md).

## Конфигурация

Данные: `%LOCALAPPDATA%/AgentsIDE/{db,artifacts,logs,secrets,runtime,temp}`, ACL
текущего пользователя. API/worker/launcher должны работать от одной учётной записи.
`.local/` внутри checkout предназначен только для изолированных тестов.

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `AGENTS_IDE_DATA_DIR` | `%LOCALAPPDATA%/AgentsIDE` | Отдельный локальный каталог |
| `AGENTS_IDE_HOST` | `127.0.0.1` | Другие адреса запрещены |
| `AGENTS_IDE_PORT` | `8765` | Порт API |
| `AGENTS_IDE_FRONTEND_DIR` | `frontend/dist` в checkout | Путь к UI; задаётся при установке wheel |
| `AGENTS_IDE_DEV_ORIGIN` | не задан | Точный Origin Vite |
| `AGENTS_IDE_RUN_ARTIFACT_BYTES` | `1073741824` | Квота артефактов Run |
| `AGENTS_IDE_DATA_BUDGET_BYTES` | `10737418240` | Общая квота данных |
| `AGENTS_IDE_DISK_RESERVE_BYTES` | `67108864` | Запас для остановки |
| `AGENTS_IDE_DETAILED_EVENTS_LIMIT` | `100000` | Подробные события Run |
| `AGENTS_IDE_RETENTION_DAYS` | `30` | Срок подробной терминальной истории |

`--data-dir` и `--port` стоят **до** подкоманды. После изменения env перезапустите
службы, чтобы API и worker получили одинаковые настройки.

## Разработка и проверки

Из backend: `uv sync --locked`, затем `uv run --locked agents-ide api` и
в отдельном терминале `uv run --locked agents-ide worker`. Для Vite задайте
API-процессу `AGENTS_IDE_DEV_ORIGIN=http://127.0.0.1:5173`, из frontend выполните
`npm.cmd run dev`. Vite проксирует /api и SSE; поставка работает только через FastAPI.

```powershell
# backend
uv run --locked ruff check src tests ../scripts
uv run --locked ruff format --check src tests ../scripts
uv run --locked mypy src
uv run --locked python ../scripts/generate_contracts.py --check
uv run --locked pytest -q -ra
uv build

# frontend
npm.cmd run lint
npm.cmd run format:check
npm.cmd test
npm.cmd run build
npx.cmd playwright install chromium
npm.cmd run test:e2e

# корень: архив с готовым UI
backend/.venv/Scripts/python.exe scripts/build_release.py --output .local/agents-ide-release.zip
```

`AGENTS_IDE_LOAD_REPORT` задаёт путь JSON-отчёта нагрузки 100 000 событий.
Реальные model probes расходуют лимиты и запускаются явной командой.
Fixtures и реальные проверки учитываются отдельно в [журнале](docs/IMPLEMENTATION_LOG.md).
