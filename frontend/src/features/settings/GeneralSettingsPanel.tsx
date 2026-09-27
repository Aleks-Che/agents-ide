import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { generalApi, type GeneralSettings } from '../../api/general'
import { connectionsApi, describeError } from '../../api/settings'
import { useCsrfToken } from '../../app/session'
import { CommitMessageFields } from './CommitMessageFields'

export function GeneralSettingsPanel() {
  const defaults = useQuery({
    queryKey: ['general-settings'],
    queryFn: generalApi.get,
  })
  return (
    <section className="panel" aria-labelledby="general-settings-heading">
      <header className="panel-header">
        <span className="section-label" id="general-settings-heading">
          Общие настройки
        </span>
      </header>
      {defaults.isLoading ? <p role="status">Загружаем настройки…</p> : null}
      {defaults.error ? (
        <p role="alert" className="error">
          {describeError(defaults.error)}
        </p>
      ) : null}
      {defaults.data ? <GeneralSettingsForm initial={defaults.data} /> : null}
    </section>
  )
}

function GeneralSettingsForm({ initial }: { initial: GeneralSettings }) {
  const csrf = useCsrfToken()
  const client = useQueryClient()
  const [value, setValue] = useState(initial)
  const connections = useQuery({
    queryKey: ['connections', { includeArchived: true }],
    queryFn: () => connectionsApi.list({ includeArchived: true }),
  })
  const save = useMutation({
    mutationFn: () => generalApi.save(value, csrf),
    onSuccess: (updated) => {
      setValue(updated)
      client.setQueryData(['general-settings'], updated)
    },
  })
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault()
        save.mutate()
      }}
    >
      <fieldset className="general-settings-fields" disabled={save.isPending}>
        <h3>Проверка рабочей копии</h3>
        <label className="checkbox-row">
          <input
            type="checkbox"
            checked={value.workspace_fingerprint_limit_mib === null}
            onChange={(event) => {
              save.reset()
              setValue({
                ...value,
                workspace_fingerprint_limit_mib: event.target.checked
                  ? null
                  : 64,
              })
            }}
          />{' '}
          Без лимита
        </label>
        <label>
          Лимит объёма файлов, МиБ
          <input
            type="number"
            min={1}
            step={1}
            required
            disabled={value.workspace_fingerprint_limit_mib === null}
            value={
              value.workspace_fingerprint_limit_mib === null
                ? ''
                : (value.workspace_fingerprint_limit_mib ?? 64)
            }
            onChange={(event) => {
              save.reset()
              setValue({
                ...value,
                workspace_fingerprint_limit_mib: Number(event.target.value),
              })
            }}
          />
        </label>
        <p className="hint">
          Общий объём файлов при проверке состояния рабочей копии во всех
          проектах. 1 МиБ = 1 048 576 байт. Игнорируемые Git файлы, которые не
          добавлены в репозиторий, не учитываются. Изменение действует при
          следующей проверке, в том числе после продолжения остановленного
          запуска.
        </p>
        <h3>Восстановление зависших агентов</h3>
        <label className="checkbox-row">
          <input
            type="checkbox"
            checked={value.harness_watchdog?.enabled ?? true}
            onChange={(event) => {
              save.reset()
              setValue({
                ...value,
                harness_watchdog: {
                  idle_minutes: value.harness_watchdog?.idle_minutes ?? 30,
                  enabled: event.target.checked,
                },
              })
            }}
          />{' '}
          Автоматически восстанавливать зависший harness
        </label>
        <label>
          Без активности, минут
          <input
            type="number"
            min={1}
            max={1440}
            step={1}
            required
            value={value.harness_watchdog?.idle_minutes ?? 30}
            disabled={value.harness_watchdog?.enabled === false}
            onChange={(event) => {
              save.reset()
              setValue({
                ...value,
                harness_watchdog: {
                  enabled: value.harness_watchdog?.enabled ?? true,
                  idle_minutes: Number(event.target.value),
                },
              })
            }}
          />
        </label>
        <p className="hint">
          Для агентов в шаблонах всех проектов. Если нет новых ответов или
          активности инструментов, приложение автоматически выполнит паузу и
          продолжит сохранённую сессию. Ожидание вашего ответа или разрешения не
          считается зависанием. Ручная пауза и остановка отменяют автоматическое
          продолжение.
        </p>
        <h3>Генерация сообщения коммита</h3>
        <p className="hint">
          Настройки по умолчанию для GitCommit. В каждом узле можно задать
          собственные значения.
        </p>
        <CommitMessageFields
          value={value.commit_message ?? {}}
          connections={connections.data ?? []}
          onChange={(commit_message) => {
            save.reset()
            setValue({ ...value, commit_message })
          }}
        />
        {connections.error ? (
          <p role="alert" className="error">
            {describeError(connections.error)}
          </p>
        ) : null}
        {save.error ? (
          <p role="alert" className="error">
            {describeError(save.error)}{' '}
            <button
              type="button"
              className="quiet"
              onClick={async () => {
                const result = await defaultsReload()
                if (result) setValue(result)
              }}
            >
              Загрузить текущие настройки
            </button>
          </p>
        ) : null}
        {save.isSuccess ? (
          <p role="status">Общие настройки сохранены.</p>
        ) : null}
        <button
          type="submit"
          disabled={!csrf || !value.commit_message?.prompt?.trim()}
        >
          Сохранить общие настройки
        </button>
      </fieldset>
    </form>
  )

  async function defaultsReload() {
    try {
      const updated = await generalApi.get()
      save.reset()
      client.setQueryData(['general-settings'], updated)
      return updated
    } catch {
      return null
    }
  }
}
