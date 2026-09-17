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
      <h3>Генерация сообщения коммита</h3>
      <p className="hint">
        Настройки по умолчанию для GitCommit. В каждом узле можно задать
        собственные значения.
      </p>
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
