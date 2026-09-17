import { useQuery } from '@tanstack/react-query'
import { generalApi, type CommitMessageSettings } from '../../api/general'
import type { ProviderConnection } from '../../api/settings'
import { describeError } from '../../api/settings'
import { CommitMessageFields } from '../settings/CommitMessageFields'
import { object } from './graph'

export function GitCommitMessageEditor({
  config,
  connections,
  onChange,
}: {
  config: Record<string, unknown>
  connections: ProviderConnection[]
  onChange: (config: Record<string, unknown>) => void
}) {
  const defaults = useQuery({
    queryKey: ['general-settings'],
    queryFn: generalApi.get,
  })
  const custom = config.message_generation !== undefined
  const effective = {
    ...defaults.data?.commit_message,
    ...object(config.message_generation),
  } as CommitMessageSettings
  return (
    <fieldset className="schema-group">
      <legend>Сообщение коммита</legend>
      <label className="checkbox-row">
        <input
          type="checkbox"
          checked={config.generate_message === true}
          onChange={(event) =>
            onChange({ ...config, generate_message: event.target.checked })
          }
        />
        Сгенерировать сообщение коммита
      </label>
      {config.generate_message === true ? (
        <>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={custom}
              disabled={!custom && !defaults.data}
              onChange={(event) => {
                const next = { ...config }
                if (event.target.checked) next.message_generation = effective
                else delete next.message_generation
                onChange(next)
              }}
            />
            Собственные настройки генерации
          </label>
          {defaults.isLoading ? (
            <p role="status">Загружаем общие настройки…</p>
          ) : null}
          {defaults.error ? (
            <p role="alert" className="error">
              {describeError(defaults.error)}
            </p>
          ) : null}
          <fieldset className="general-settings-fields" disabled={!custom}>
            <CommitMessageFields
              value={effective}
              connections={connections}
              onChange={(message_generation) =>
                onChange({ ...config, message_generation })
              }
            />
          </fieldset>
          {!custom ? (
            <p className="hint">
              Используются общие настройки. Включите собственные настройки,
              чтобы изменить их только для этого узла.
            </p>
          ) : null}
        </>
      ) : null}
    </fieldset>
  )
}
