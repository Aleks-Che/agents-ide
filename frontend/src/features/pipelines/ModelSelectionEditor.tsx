import { useId } from 'react'
import type { EditorResources } from './resources'
import { object } from './graph'

export function ModelSelectionEditor({
  kind,
  value,
  onChange,
  resources,
  inherited,
  label = 'Модель',
  allowInherit = true,
}: {
  kind: 'agent' | 'llm'
  value: unknown
  onChange: (value: Record<string, unknown> | undefined) => void
  resources: EditorResources
  inherited?: unknown
  label?: string
  allowInherit?: boolean
}) {
  const id = useId()
  const selection = object(value)
  const effective = value ? selection : object(inherited)
  const group = resources.groups.find((g) => g.id === effective.group_id)
  const resourcesList =
    kind === 'agent' ? resources.harnesses : resources.connections
  const resourceKey =
    kind === 'agent' ? 'harness_profile_id' : 'provider_connection_id'
  const resource = resourcesList.find((r) => r.id === effective[resourceKey])
  const models = resource
    ? [
        ...new Set([
          ...(resource.catalog_models ?? []),
          ...('manual_models' in resource ? resource.manual_models : []),
        ]),
      ]
    : []
  return (
    <fieldset className="schema-group">
      <legend>
        {label} · {kind}
      </legend>
      <label className="schema-field">
        Выбор модели
        <select
          value={String(selection.kind ?? '')}
          onChange={(e) => {
            onChange(
              e.target.value === 'group'
                ? { kind: 'group', group_id: '' }
                : e.target.value === 'direct'
                  ? { kind: 'direct', [resourceKey]: '', model_id: '' }
                  : undefined,
            )
          }}
        >
          <option value="">
            {allowInherit ? 'Наследовать роль / привязку' : 'Выберите способ…'}
          </option>
          <option value="direct">Конкретная модель (direct)</option>
          <option value="group">Группа (group)</option>
        </select>
      </label>
      {selection.kind === 'group' && (
        <label className="schema-field">
          Группа моделей
          <select
            value={String(selection.group_id ?? '')}
            onChange={(e) =>
              onChange({ kind: 'group', group_id: e.target.value })
            }
          >
            <option value="">Выберите группу {kind}…</option>
            {selection.group_id &&
            !resources.groups.some(
              (g) => g.id === selection.group_id && g.kind === kind,
            ) ? (
              <option value={String(selection.group_id)}>
                Не разрешена: {String(selection.group_id)}
              </option>
            ) : null}
            {resources.groups
              .filter((g) => g.kind === kind)
              .map((g) => (
                <option key={g.id} value={g.id} disabled={g.archived}>
                  {g.name}
                  {g.archived ? ' (архив)' : ''}
                </option>
              ))}
          </select>
        </label>
      )}
      {selection.kind === 'direct' && (
        <>
          <label className="schema-field">
            {kind === 'agent' ? 'Профиль harness' : 'LLM-подключение'}
            <select
              value={String(selection[resourceKey] ?? '')}
              onChange={(e) =>
                onChange({ ...selection, [resourceKey]: e.target.value })
              }
            >
              <option value="">Выберите…</option>
              {selection[resourceKey] &&
              !resourcesList.some((r) => r.id === selection[resourceKey]) ? (
                <option value={String(selection[resourceKey])}>
                  Не разрешён: {String(selection[resourceKey])}
                </option>
              ) : null}
              {resourcesList.map((r) => (
                <option key={r.id} value={r.id} disabled={r.archived}>
                  {r.name}
                  {r.archived ? ' (архив)' : ''}
                </option>
              ))}
            </select>
          </label>
          <label className="schema-field">
            ID модели
            <input
              list={`${id}-models`}
              value={String(selection.model_id ?? '')}
              onChange={(e) =>
                onChange({ ...selection, model_id: e.target.value })
              }
            />
          </label>
          <datalist id={`${id}-models`}>
            {models.map((m) => (
              <option key={m} value={m} />
            ))}
          </datalist>
        </>
      )}
      <p className="hint">
        Источник:{' '}
        {value
          ? 'узел / текущая настройка'
          : inherited
            ? 'роль шаблона'
            : 'привязка → настройки запуска'}
        . Узел имеет приоритет над ролью. Параметры: профиль → кандидат → роль →
        узел.
      </p>
      {group && (
        <ol className="candidate-preview" aria-label="Приоритет кандидатов">
          {[...group.members]
            .sort((a, b) => a.member_index - b.member_index)
            .map((m) => (
              <li key={m.id}>
                <strong>
                  {m.member_index + 1}. {m.model_id}
                </strong>{' '}
                · {m.enabled ? 'включён' : 'отключён'}
                <Destination
                  resources={resources}
                  harnessId={m.harness_profile_id}
                  connectionId={m.provider_connection_id}
                />
                <small>Параметры кандидата: {JSON.stringify(m.params)}</small>
              </li>
            ))}
        </ol>
      )}
      {effective.kind === 'direct' && (
        <Destination
          resources={resources}
          harnessId={effective.harness_profile_id as string}
          connectionId={effective.provider_connection_id as string}
        />
      )}
      <small className="muted">
        Capability модели проверяется preflight перед запуском; наличие в
        каталоге не подтверждает поддержку параметров и прав.
      </small>
    </fieldset>
  )
}

export function Destination({
  resources,
  harnessId,
  connectionId,
}: {
  resources: EditorResources
  harnessId?: string | null
  connectionId?: string | null
}) {
  const harness = resources.harnesses.find((h) => h.id === harnessId)
  const connection = resources.connections.find((c) => c.id === connectionId)
  return (
    <div className="destination">
      {harnessId && (
        <>
          <span>
            {harness?.name ?? `Неизвестный профиль ${harnessId}`}
            {harness?.archived ? ' (архив)' : ''}
          </span>
          <code>{harness?.executable_path ?? 'Программа из PATH'}</code>
          {harness && (
            <details>
              <summary>Параметры запуска harness</summary>
              <pre>{JSON.stringify(harness.settings, null, 2)}</pre>
            </details>
          )}
        </>
      )}
      {connectionId && (
        <>
          <span>
            {connection?.name ?? `Неизвестное подключение ${connectionId}`}
            {connection?.archived ? ' (архив)' : ''}
          </span>
          <code>{connection?.base_url}</code>
        </>
      )}
    </div>
  )
}
