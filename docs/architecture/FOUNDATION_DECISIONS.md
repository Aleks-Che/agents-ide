# Этапы 0–1: принятые решения

Дата: 2026-09-14. Статус: каркас реализован; ограничения реальных harness в [CAPABILITIES](../integrations/CAPABILITIES.md).

## Стек и границы

Принят стек плана: FastAPI/Pydantic, SQLAlchemy/SQLite, Alembic, отдельный Python worker; React/TypeScript/Vite, TanStack Query и установленный React Flow. Точные прямые и транзитивные версии — в `backend/pyproject.toml`, `backend/uv.lock`, `frontend/package.json`, `frontend/package-lock.json`.

Миграции находятся в `backend/src/agents_ide/persistence/migrations`, чтобы попадать в wheel. Отдельный корневой `backend/alembic` из предварительной структуры не нужен. Таблицы этапа 1: pairing, auth_sessions, worker_heartbeat; доменные таблицы создаются на этапе 2. Backend wheel проверяется отдельно от сборки frontend; при установке wheel путь к внешнему `frontend/dist` задаётся настройкой.

## Прототипы и проверенные контракты

| Контракт | Реализация / проверка | Граница |
| --- | --- | --- |
| SQLite | WAL, foreign_keys, busy_timeout=5000, короткие транзакции, Alembic под межпроцессным lock | QueueJob/lease/generation ещё не реализованы |
| Состояния | Типизированный каталог RunState и консервативный capability gate | Полная машина переходов и команды — этапы 4–5; enum не заменяет её |
| Auth | 5 минут/5 попыток, интервал между ошибками 1 с; сессия 12 ч, hashed bearer ID, CSRF, revoke | Нет сетевого режима/TLS |
| Секреты | DPAPI user scope, immutable UUID reference, ошибка без удаления ciphertext, редактирование логов | API настройки подключения/ротации ссылок — этап 2 |
| SSE | Авторизованные ограниченные снимки, повторная проверка сессии каждую секунду | Durable Run events/replay/backpressure gate — этап 4 |
| Процессы | Suspended CreateProcess → Job → ResumeThread, kill-on-close, без breakaway/наследования Job handle | ProcessSupervisor на Run и recovery журнала — этап 5 |
| Launcher | Независимый daemon, start/status/stop, lock, PID+creation time+exe, 3 ограниченных restart | Autostart/обновления/активные Run — последующие этапы |
| Пути | Ограничение data dir локальным диском, отказ от ссылок, identity `(st_dev, st_ino)` через Windows stat | Полная проверка workspace handles, junction race, subst/8.3 и резерваций — этапы 2/5 |
| Git | Временный репозиторий с кириллицей/пробелами, baseline и обнаружение dirty | Автокоммит, hooks/signing и temporary index — этап 8 |

На Windows у venv launcher есть промежуточный процесс. Для подтверждения запуска используется случайный `launch_id`, а не предположение, что PID оболочки Python совпадает с PID рабочего интерпретатора. Для stop всегда дополнительно проверяется OS creation time и executable.

DPAPI требует загруженного профиля Windows. Тест с синтетическим значением под sandbox-пользователем без профиля вернул системную ошибку; под пользователем запуска приложения roundtrip и сохранение недоступного ciphertext прошли. Никакой fallback на plaintext не добавлен.

## Безопасность следующих этапов

Правила STATE_MACHINES, RUNTIME_CONTRACTS, EXECUTION_CONTRACTS и SECURITY_AND_OPERATIONS сохраняются. Обнаруженных оснований ослабить их нет. Наличие работающего HTTP/stdin транспорта не подтверждает изоляцию агентной записи.

`dirty_policy=allow_nonoverlap` **не допускается** до реальных тестов baseline, allowlist, временного index, hooks/signing, пересечений untracked/rename/delete и внешней правки. Пока единственная разрешённая будущая политика автономного пресета — `run_branch/strict`; сама Git-операция ещё не реализована.

Негативный набор зафиксирован в [security-cases.json](../integrations/fixtures/security-cases.json). В этапе 1 проверяются Host/Origin/CSRF, pairing/session expiry, занятый порт, DPAPI и смерть владельца Job. Импорт графа, контекст, TOCTOU, hooks и восстановление неизвестных внешних эффектов остаются обязательными тестами соответствующих этапов.

## Эталон нагрузки

Целевой профиль: Windows x64 + локальный NTFS/SSD, 4 CPU, 8 GiB RAM, один API/worker, максимум 2 Run; 100 000 событий, payload до 16 KiB, артефакт 10 MiB, медленный SSE-клиент. Цели p95: snapshot <500 мс, первая страница истории <1 с, доставка сохранённого события <1 с, ограниченная память клиента/сервера. Замеры этого профиля **ещё не выполнены**, поскольку журнал Run и артефакты относятся к следующим этапам; это gate этапа 12, а не утверждение о производительности каркаса.

Основание работы Job и DPAPI сверено с [Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects) и [CryptProtectData](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata). Проверка поведения выполнена отдельно реальными Windows-тестами.
