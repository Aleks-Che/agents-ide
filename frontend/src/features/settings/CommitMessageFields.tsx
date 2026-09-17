import { useId } from 'react'
import type { CommitMessageSettings } from '../../api/general'
import type { ProviderConnection } from '../../api/settings'
import type { Schema } from '../../api/graphs'
import { ObjectFields } from '../pipelines/SchemaFields'
import { MODEL_PARAM_DESCRIPTORS } from './model_params'

const paramsSchema: Schema = {
  type: 'object',
  additionalProperties: false,
  properties: Object.fromEntries(
    MODEL_PARAM_DESCRIPTORS.map((item) => [
      item.name,
      {
        type:
          item.kind === 'enum'
            ? 'string'
            : item.kind === 'strings'
              ? 'array'
              : item.kind,
        ...(item.options ? { enum: [...item.options] } : {}),
        ...(item.kind === 'strings' ? { items: { type: 'string' } } : {}),
        ...(item.min !== undefined ? { minimum: item.min } : {}),
        ...(item.max !== undefined ? { maximum: item.max } : {}),
      },
    ]),
  ),
}

export function CommitMessageFields({
  value,
  connections,
  onChange,
}: {
  value: CommitMessageSettings
  connections: ProviderConnection[]
  onChange: (value: CommitMessageSettings) => void
}) {
  const id = useId()
  const connection = connections.find((item) => item.id === value.connection_id)
  const models = [
    ...new Set([
      ...(connection?.catalog_models ?? []),
      ...(connection?.manual_models ?? []),
    ]),
  ]
  return (
    <div className="commit-message-fields">
      <label>
        LLM-подключение
        <select
          aria-label="LLM-подключение"
          value={value.connection_id ?? ''}
          onChange={(event) =>
            onChange({ ...value, connection_id: event.target.value, model: '' })
          }
        >
          <option value="">Выберите подключение</option>
          {value.connection_id && !connection ? (
            <option value={value.connection_id}>Подключение недоступно</option>
          ) : null}
          {connections.map((item) => (
            <option key={item.id} value={item.id} disabled={item.archived}>
              {item.name}
              {item.archived ? ' (архив)' : ''}
            </option>
          ))}
        </select>
      </label>
      <label>
        Модель для сообщения коммита
        <input
          list={`${id}-models`}
          value={value.model ?? ''}
          maxLength={256}
          onChange={(event) =>
            onChange({ ...value, model: event.target.value })
          }
        />
        <datalist id={`${id}-models`}>
          {models.map((model) => (
            <option key={model} value={model} />
          ))}
        </datalist>
      </label>
      <label>
        Язык сообщения коммита
        <select
          aria-label="Язык сообщения коммита"
          value={value.language ?? 'ru'}
          onChange={(event) =>
            onChange({ ...value, language: event.target.value as 'ru' | 'en' })
          }
        >
          <option value="ru">Русский</option>
          <option value="en">English</option>
        </select>
      </label>
      <label>
        Промпт сообщения коммита
        <textarea
          rows={9}
          value={value.prompt ?? ''}
          required
          maxLength={8000}
          onChange={(event) =>
            onChange({ ...value, prompt: event.target.value })
          }
        />
      </label>
      <p className="hint">
        Diff добавляется автоматически. Для удалённых файлов передаются только
        их имена и факт удаления. Тип и область коммита остаются на английском.
      </p>
      <ObjectFields
        label="Параметры модели"
        schema={paramsSchema}
        value={value.params ?? {}}
        onChange={(params) => onChange({ ...value, params })}
      />
    </div>
  )
}
