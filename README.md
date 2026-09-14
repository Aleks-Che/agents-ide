# Agents IDE

Локальная рабочая область для проектов, чатов и процессов с агентами. Реализованы каркас **этапа 1**, конфигурационные API **этапа 2** и группы моделей **этапа 2A**: FastAPI, независимый worker, SQLite, локальный вход, DPAPI и React-интерфейс состояния служб. Результаты реальных проверок **этапа 0** и оставшиеся `unverified` перечислены в [матрице интеграций](docs/integrations/CAPABILITIES.md).

Проекты, чаты и конфигурация pipeline доступны через API. UI этих функций и выполнение задач появятся на следующих этапах [плана](docs/IMPLEMENTATION_PLAN.md). Worker пока сохраняет heartbeat; очередь хранится в БД и ожидает реализации исполнителя.

[Журнал реализации](docs/IMPLEMENTATION_LOG.md): нюансы разработки, фичи, открытые блокеры, результаты проверок и заметки для следующих этапов.

Этап 2 добавляет защищённые API проектов/чатов, черновики и версии pipeline, настройки подключений, снимки Run и журнал команд. [Доменный контракт](docs/architecture/DOMAIN_DATA.md) описывает API, приоритет настроек, генерацию TypeScript и обновление ранее созданных данных. Выполнение очереди появится на этапах 4–5.

Этап 2A добавляет [группы моделей](docs/architecture/MODEL_GROUPS.md): упорядоченные agent/llm-кандидаты, выбор direct/group для ролей и узлов, наследование параметров, полный снимок исполнителей и перенос определений групп. Выбор кандидата и fallback worker появятся на этапах 4–7.

## Среда

- Основная среда: Windows x64, локальный NTFS, обычная учётная запись с загруженным профилем. Проверено на Windows 10 22H2, build 19045; Windows CI включён отдельно от Linux.
- Python **3.12–3.13**, рекомендуемый и проверенный локально **3.12.7**; `uv` **0.9.15**.
- Node **22.12+ в ветке 22**, проверен **22.20.0**; npm **10–11**, проверен **11.10.0**. Node нужен для разработки и сборки; готовый UI отдаёт Python.
- Git **2.39.1+**. Codex/OpenCode не нужны для запуска каркаса.
- Linux — дополнительная среда разработки/CI. SecretStore на Linux возвращает `secret_unavailable`: plaintext-замены DPAPI нет.

## Установка и запуск

Из корня репозитория, PowerShell:

```powershell
cd backend
uv sync --locked
cd ../frontend
npm.cmd ci
npm.cmd run build
cd ../backend
uv run --locked agents-ide migrate
uv run --locked agents-ide start
uv run --locked agents-ide auth pair-code
```

Откройте **http://127.0.0.1:8765** и введите выданный код. Он одноразовый и действует 5 минут. Срок сессии — 12 часов. Код отображается только командой `auth pair-code`; в журналы он не записывается.

```powershell
uv run --locked agents-ide status
uv run --locked agents-ide stop
uv run --locked agents-ide auth pair-code --rotate
```

`start` запускает скрытый launcher и отдельные API/worker под текущим пользователем. Они переживают закрытие вызывающего терминала. `stop` останавливает только экземпляр из выбранного каталога данных. Автозапуск при входе в Windows не устанавливается. Работа после выхода пользователя из Windows пока не поддерживается.

API и worker можно запустить независимо в двух терминалах из `backend`:

```powershell
uv run --locked agents-ide api
```

```powershell
uv run --locked agents-ide worker
```

Ручные экземпляры завершаются через Ctrl+C. При запуске API и worker применяются миграции; отдельная `migrate` полезна для диагностики. Одновременно допускается один API и один worker на каталог данных.

## Разработка frontend

Для Vite укажите точный разрешённый Origin в терминале API:

```powershell
# backend
$env:AGENTS_IDE_DEV_ORIGIN = 'http://127.0.0.1:5173'
uv run --locked agents-ide api
```

В другом терминале:

```powershell
# frontend
npm.cmd run dev
```

Откройте **http://127.0.0.1:5173**. Vite проксирует `/api`, включая SSE; браузер использует same-origin cookie. При смене API-порта задайте одинаковый `AGENTS_IDE_PORT` в обоих терминалах. Для разработки изменённый Python-код требует перезапуска API/worker.

## Данные и конфигурация

По умолчанию: `%LOCALAPPDATA%/AgentsIDE/{db,artifacts,logs,secrets,runtime,temp}`. Каталог защищён ACL текущего пользователя. Запускайте службы от одной учётной записи; DPAPI использует её Windows-профиль.

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `AGENTS_IDE_DATA_DIR` | `%LOCALAPPDATA%/AgentsIDE` | Каталог данных вне управляемых репозиториев |
| `AGENTS_IDE_PORT` | `8765` | Порт API; занятой порт не освобождается принудительно |
| `AGENTS_IDE_HOST` | `127.0.0.1` | Другие адреса в v1 отклоняются |
| `AGENTS_IDE_DEV_ORIGIN` | не задан | Точный Origin Vite с портом |
| `AGENTS_IDE_FRONTEND_DIR` | `frontend/dist` в checkout | Каталог собранного UI; задайте явно при установке wheel |
| `AGENTS_IDE_LOG_LEVEL` | `INFO` | Уровень структурированных журналов |

Также доступны общие CLI-параметры **до** подкоманды:

```powershell
uv run --locked agents-ide --data-dir C:/AgentsIDE-data --port 8877 start
uv run --locked agents-ide --data-dir C:/AgentsIDE-data --port 8877 auth pair-code
uv run --locked agents-ide --data-dir C:/AgentsIDE-data --port 8877 stop
```

При обычном запуске используйте каталог вне Git-репозитория. `.local/` предназначен только для изолированных проверок разработки и исключён из Git. Не копируйте одну live `.db` как резервную копию WAL-базы; штатный backup запланирован на этап 12.

## API каркаса

| Endpoint | Доступ | Результат |
| --- | --- | --- |
| `GET /api/health` | Без сессии | Только факт работы HTTP API |
| `POST /api/auth/pair` | Origin + одноразовый код в JSON | HttpOnly/SameSite=Strict cookie и CSRF-токен |
| `GET /api/auth/session` | Сессия | Срок и CSRF-токен |
| `POST /api/auth/logout` | Сессия + Origin + `X-CSRF-Token` | Отзыв текущей сессии |
| `GET /api/readiness` | Сессия | 200 при готовой БД и свежем heartbeat; иначе 503 |
| `GET /api/system/status` | Сессия | Раздельное состояние API, БД и worker |
| `GET /api/system/events` | Сессия | SSE-снимки состояния; отзыв/истечение закрывает поток |
| `GET /api/openapi.json` | Сессия | Схема API |

`system/events` пока не является журналом Run: курсор, replay и общий poller появятся на этапе 4. Ошибки имеют форму `{code, message, details, request_id, retryable}`. Host и Origin проверяются также для pairing и SSE. Cookie применяется только к `/api`; сервер работает по HTTP на loopback, без заявления о TLS.

## Проверки

Из `backend`:

```powershell
uv run --locked ruff check src tests
uv run --locked ruff format --check src tests
uv run --locked mypy src
uv run --locked pytest -q -ra
uv build
```

Из `frontend`:

```powershell
npm.cmd run lint
npm.cmd run format:check
npm.cmd test
npm.cmd run build
npx.cmd playwright install chromium
npm.cmd run test:e2e
```

Windows-проверки действительно используют DPAPI, ACL, Job Objects и дочерние процессы; пропуск на Linux не считается Windows-проверкой. Playwright запускает отдельный API на порту 18767. [CI](.github/workflows/ci.yml) проверяет backend на Windows/Linux и сборку/UI на Windows. Скрипты `scripts/setup.ps1`, `scripts/check.ps1`, `scripts/agents-ide.ps1` сокращают эти команды; менять глобальную ExecutionPolicy не требуется — можно выполнить команды напрямую.

Повторение платных smoke-запросов к harness выполняется отдельной явной командой, см. [CAPABILITIES](docs/integrations/CAPABILITIES.md). [Принятые решения и границы этапов](docs/architecture/FOUNDATION_DECISIONS.md) отделяют реализованные механизмы от будущего движка.
