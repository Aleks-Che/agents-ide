import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  applicationLogs,
  type ApplicationLogs,
  type LogLevel,
  type LogRole,
  type LogScope,
} from '../../api/system'

const descriptions: Record<string, string> = {
  'database.schema_mismatch':
    'Версия базы данных не совпадает с версией приложения.',
  'worker.database_unavailable':
    'Исполнитель остановился после трёх неудачных проверок базы данных.',
  'worker.database_check_failed': 'Не удалось проверить базу данных.',
  'launcher.startup_timeout':
    'Службы не успели запуститься и были остановлены.',
  'launcher.child_exited':
    'Одна из служб завершилась. Launcher пытается её перезапустить.',
  'launcher.failed': 'Launcher остановился с ошибкой.',
  startup_failed: 'Не удалось запустить приложение.',
  'service.unexpected_error':
    'Служба остановилась из-за непредвиденной ошибки.',
  'request.failed': 'Ошибка при обработке запроса.',
}

function downloadLogs(report: ApplicationLogs) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' }),
  )
  const link = document.createElement('a')
  link.href = url
  link.download = 'agents-ide-logs.json'
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export function ApplicationLogsPanel() {
  const [role, setRole] = useState<LogRole>('all')
  const [level, setLevel] = useState<LogLevel>('WARNING')
  const [scope, setScope] = useState<LogScope>('current')
  const logs = useQuery({
    queryKey: ['application-logs', role, level, scope],
    queryFn: () => applicationLogs(role, level, scope),
    refetchInterval: 15_000,
  })
  return (
    <section
      className="panel application-logs"
      aria-labelledby="application-logs-heading"
    >
      <header className="panel-header">
        <span className="section-label" id="application-logs-heading">
          Журнал приложения
        </span>
        <div className="button-row">
          <button
            type="button"
            className="quiet"
            disabled={logs.isFetching}
            onClick={() => void logs.refetch()}
          >
            {logs.isFetching ? 'Загрузка…' : 'Обновить журнал'}
          </button>
          <button
            type="button"
            className="quiet"
            disabled={!logs.data}
            onClick={() => logs.data && downloadLogs(logs.data)}
          >
            Скачать журнал
          </button>
        </div>
      </header>
      <p className="hint">
        По умолчанию показаны записи текущего запуска. Ошибки прошлых запусков
        сохраняются в истории и не означают, что приложение сейчас неисправно.
      </p>
      <div className="log-filters">
        <label>
          Период
          <select
            value={scope}
            onChange={(event) => setScope(event.target.value as LogScope)}
          >
            <option value="current">Текущий запуск</option>
            <option value="all">Вся история</option>
          </select>
        </label>
        <label>
          Служба
          <select
            value={role}
            onChange={(event) => setRole(event.target.value as LogRole)}
          >
            <option value="all">Все службы</option>
            <option value="launcher">Launcher</option>
            <option value="api">API</option>
            <option value="worker">Исполнитель</option>
            <option value="start">Запуск приложения</option>
            <option value="stop">Остановка приложения</option>
            <option value="migrate">Обновление базы</option>
            <option value="auth">Подключение браузера</option>
          </select>
        </label>
        <label>
          Уровень записей
          <select
            value={level}
            onChange={(event) => setLevel(event.target.value as LogLevel)}
          >
            <option value="WARNING">Ошибки и предупреждения</option>
            <option value="ERROR">Только ошибки</option>
            <option value="INFO">Все события</option>
          </select>
        </label>
      </div>
      {logs.data?.current_started_at ? (
        <p className="hint">
          Текущий запуск с{' '}
          {new Date(logs.data.current_started_at).toLocaleString('ru-RU')}.
        </p>
      ) : null}
      <p className="hint">
        Если сервер выключен, выполните из папки приложения:{' '}
        <code>./scripts/agents-ide.ps1 logs</code>
      </p>
      {logs.data ? (
        <p className="hint log-directory">
          Папка журналов: <code>{logs.data.directory}</code>
        </p>
      ) : null}
      {logs.error ? (
        <p className="error" role="alert">
          Не удалось обновить журнал. Сохранённые записи ниже остаются доступны;
          журнал на диске можно прочитать командой выше.
        </p>
      ) : null}
      {logs.isLoading ? <p role="status">Загружаем журнал…</p> : null}
      {logs.data?.entries.length === 0 ? (
        <p role="status">
          {logs.data.scope === 'current'
            ? level === 'WARNING' && role === 'all'
              ? 'В журнале текущего запуска ошибок и предупреждений нет.'
              : 'В текущем запуске записей выбранного уровня нет.'
            : 'Записей выбранного уровня нет.'}
        </p>
      ) : null}
      {logs.data?.truncated ? (
        <p className="hint">
          Показаны последние записи. Полный журнал хранится в указанной папке.
        </p>
      ) : null}
      <ol className="log-entries" aria-label="Записи журнала">
        {logs.data?.entries.map((entry, index) => (
          <li
            key={`${entry.at}-${entry.service}-${index}`}
            className={
              entry.level === 'ERROR' || entry.level === 'CRITICAL'
                ? 'log-error'
                : ''
            }
          >
            <div className="log-meta">
              <time dateTime={entry.at}>
                {new Date(entry.at).toLocaleString('ru-RU')}
              </time>{' '}
              · {entry.service} · {entry.level}
              {logs.data?.current_started_at &&
              new Date(entry.at) < new Date(logs.data.current_started_at)
                ? ' · Прошлый запуск'
                : null}
            </div>
            {descriptions[entry.message] ? (
              <p>{descriptions[entry.message]}</p>
            ) : null}
            <code>{entry.message}</code>
            {Object.keys(entry.details ?? {}).length > 0 ? (
              <details>
                <summary>Подробности</summary>
                <pre>{JSON.stringify(entry.details, null, 2)}</pre>
              </details>
            ) : null}
          </li>
        ))}
      </ol>
    </section>
  )
}
