import { useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { ApiError } from '../../api/client'
import {
  bindingsApi,
  templatesApi,
  type PipelineBinding,
} from '../../api/bindings'
import { useCsrfToken } from '../../app/session'
import { Modal } from '../../app/Modal'
import { EditorError } from '../settings/EditorError'
import { formatDateTime, shortHash } from '../../app/format'
import { WorkspacePolicyFields } from './WorkspacePolicyFields'
import {
  BindingSelectionDraft,
  buildDraftFromSelection,
  formatSettingSource,
  formatWarningCode,
  serialiseSelection,
  parseLimitOverrides,
  graphRoles,
} from './utils'

export interface BindingEditorResources {
  groups: {
    agent: Array<{
      id: string
      name: string
      members: Array<{
        id: string
        member_index: number
        enabled: boolean
        harness_profile_id: string | null
        provider_connection_id: string | null
        model_id: string
      }>
      archived: boolean
      revision: number
    }>
    llm: Array<{
      id: string
      name: string
      members: Array<{
        id: string
        member_index: number
        enabled: boolean
        harness_profile_id: string | null
        provider_connection_id: string | null
        model_id: string
      }>
      archived: boolean
      revision: number
    }>
  }
  harnesses: Array<{ id: string; name: string; archived: boolean }>
  connections: Array<{
    id: string
    name: string
    archived: boolean
    manual_models: string[]
    catalog_models: string[]
  }>
}

interface BindingEditorProps {
  binding: PipelineBinding | null
  resources: BindingEditorResources
  onClose: () => void
  onSaved: (binding: PipelineBinding) => void
}

export function BindingEditor({
  binding,
  resources,
  onClose,
  onSaved,
}: BindingEditorProps) {
  const [reload, setReload] = useState(0)
  const detail = useQuery({
    queryKey: ['binding_edit', binding?.id, reload],
    queryFn: () => bindingsApi.get(binding!.id),
    enabled: Boolean(binding),
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    staleTime: Infinity,
    refetchOnMount: 'always',
  })
  if (!detail.data || !detail.isFetchedAfterMount)
    return (
      <Modal onClose={onClose} busy={false} label="Загрузка привязки">
        <div className="dialog">
          <button type="button" className="quiet" onClick={onClose}>
            Закрыть
          </button>
          {detail.error ? (
            <p role="alert" className="error">
              {renderBindingError(detail.error)}
            </p>
          ) : (
            <p role="status">Загружаем привязку…</p>
          )}
        </div>
      </Modal>
    )
  return (
    <BindingEditorForm
      key={reload}
      binding={detail.data}
      resources={resources}
      onClose={onClose}
      onSaved={onSaved}
      onReload={() => setReload((value) => value + 1)}
    />
  )
}

function BindingEditorForm({
  binding,
  resources,
  onClose,
  onSaved,
  onReload,
}: BindingEditorProps & { onReload: () => void }) {
  const csrf = useCsrfToken()
  const resolved = useQuery({
    queryKey: [
      'binding_resolved',
      { bindingId: binding?.id ?? '', version: binding?.version },
    ],
    queryFn: () => bindingsApi.resolved(binding!.id),
    enabled: Boolean(binding),
    refetchOnMount: 'always',
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })
  const version = useQuery({
    queryKey: ['version', binding?.version_id],
    queryFn: () => templatesApi.getVersion(binding!.version_id),
    enabled: Boolean(binding),
  })
  const roleMap = version.data ? graphRoles(version.data.graph) : {}
  const [name, setName] = useState(binding?.name ?? '')
  const [branchPolicy, setBranchPolicy] = useState<'run_branch' | 'current'>(
    binding?.branch_policy ?? 'run_branch',
  )
  const [workspaceMode, setWorkspaceMode] = useState<'project' | 'worktree'>(
    binding?.workspace_mode ?? 'project',
  )
  const [dirtyPolicy, setDirtyPolicy] = useState<'strict' | 'allow_nonoverlap'>(
    binding?.dirty_policy ?? 'strict',
  )
  const limitOverridesText = '{}'
  const [selections, setSelections] = useState<
    Record<string, BindingSelectionDraft>
  >(() => initialSelections(binding))
  const preflight = useMutation({
    mutationFn: () => bindingsApi.preflight(binding!.id, csrf),
  })

  const save = useMutation({
    mutationFn: () => {
      if (!binding) throw new Error('binding is required')
      const selectionsPayload: Record<
        string,
        | {
            kind: 'direct'
            model_id: string
            harness_profile_id: string
            provider_connection_id?: undefined
          }
        | {
            kind: 'direct'
            model_id: string
            harness_profile_id?: undefined
            provider_connection_id: string
          }
        | { kind: 'group'; group_id: string }
      > = {}
      for (const [role, draft] of Object.entries(selections)) {
        const kind = roleMap[role]?.kind
        if (!kind && draft.kind) throw new Error(`Неизвестная роль ${role}`)
        const serialised = serialiseSelection(draft, kind ?? 'agent')
        if (!serialised) continue
        if (serialised.kind === 'group') {
          selectionsPayload[role] = {
            kind: 'group',
            group_id: serialised.group_id!,
          }
        } else if (kind === 'agent') {
          selectionsPayload[role] = {
            kind: 'direct',
            model_id: serialised.model_id!,
            harness_profile_id: serialised.harness_profile_id!,
          }
        } else {
          selectionsPayload[role] = {
            kind: 'direct',
            model_id: serialised.model_id!,
            provider_connection_id: serialised.provider_connection_id!,
          }
        }
      }
      return bindingsApi.update(
        binding.id,
        {
          expected_version: binding.version,
          name: name.trim(),
          branch_policy: branchPolicy,
          workspace_mode: workspaceMode,
          dirty_policy: dirtyPolicy,
          model_selections: selectionsPayload,
          model_overrides: Object.fromEntries(
            Object.entries(binding.model_overrides).filter(
              ([role]) => !(role in selections),
            ),
          ),
          limit_overrides: parseLimitOverrides(limitOverridesText),
        },
        csrf,
      )
    },
    onSuccess: (updated) => {
      onSaved(updated)
    },
  })

  const archive = useMutation({
    mutationFn: () => {
      if (!binding) throw new Error('binding is required')
      return bindingsApi.archive(binding.id, binding.version, csrf)
    },
    onSuccess: (updated) => {
      onSaved(updated)
    },
  })

  const warnings = useMemo(() => {
    if (!resolved.data) return []
    if (
      JSON.stringify(selections) === JSON.stringify(initialSelections(binding))
    )
      return resolved.data.warnings ?? []
    const implementer =
      selections['implementer'] ?? resolved.data.roles?.implementer?.selection
    const verifier =
      selections['verifier'] ?? resolved.data.roles?.verifier?.selection
    if (
      implementer?.kind === 'direct' &&
      verifier?.kind === 'direct' &&
      implementer.model_id &&
      implementer.model_id === verifier.model_id
    ) {
      return [
        {
          code: 'implementer_verifier_same_model',
          message:
            'Реализация и проверка используют одну и ту же модель. Разделение ролей всё равно работает, но перекрёстная проверка слабее, чем при разных моделях.',
          roles: ['implementer', 'verifier'],
        },
      ]
    }
    return []
  }, [resolved.data, selections, binding])

  const roles = useMemo(() => {
    return Object.entries(version.data ? graphRoles(version.data.graph) : {})
  }, [version.data])

  if (!binding) {
    return (
      <Modal onClose={onClose} busy={false} labelledBy="binding-edit-title">
        <div className="dialog">
          <header>
            <h3 id="binding-edit-title">Привязка недоступна</h3>
            <button type="button" className="quiet" onClick={onClose}>
              ×
            </button>
          </header>
          <p>Привязка не найдена или была удалена.</p>
        </div>
      </Modal>
    )
  }

  return (
    <Modal
      onClose={onClose}
      busy={save.isPending || archive.isPending || resolved.isLoading}
      labelledBy="binding-edit-title"
    >
      <div className="dialog wide binding-editor">
        <header>
          <h3 id="binding-edit-title">Привязка: {binding.name}</h3>
          <button type="button" className="quiet" onClick={onClose}>
            ×
          </button>
        </header>
        <p className="hint">
          Снимок Run остаётся неизменным. Правки действуют только на новые
          запуски. Изменение группы не обновляет активные Run; используется
          сохранённый в snapshot порядок.
        </p>
        <fieldset>
          <label htmlFor="binding-edit-name">Название</label>
          <input
            id="binding-edit-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            maxLength={120}
            required
          />
        </fieldset>
        <div className="binding-policy">
          <WorkspacePolicyFields
            workspaceMode={workspaceMode}
            branchPolicy={branchPolicy}
            onWorkspaceMode={setWorkspaceMode}
            onBranchPolicy={setBranchPolicy}
          />
          <fieldset>
            <label htmlFor="binding-dirty-policy">Грязный каталог</label>
            <select
              id="binding-dirty-policy"
              value={dirtyPolicy}
              onChange={(event) =>
                setDirtyPolicy(
                  event.target.value as 'strict' | 'allow_nonoverlap',
                )
              }
            >
              <option value="strict">strict</option>
              <option value="allow_nonoverlap">
                allow_nonoverlap (read-only)
              </option>
            </select>
          </fieldset>
        </div>
        <section className="binding-roles" aria-label="Роли и выбор моделей">
          <header>
            <span>Роли и выбор моделей</span>
            <span className="muted">
              Узел переопределяется целиком, список не объединяется.
            </span>
          </header>
          {version.isLoading ? (
            <p role="status">Загружаем резолюцию…</p>
          ) : version.error ? (
            <p className="error">{renderBindingError(version.error)}</p>
          ) : roles.length === 0 ? (
            <p className="panel-empty">
              Граф этого шаблона не объявляет ролей. Выбор моделей не требуется.
            </p>
          ) : (
            <ol className="binding-role-list">
              {roles.map(([role, assignment]) => {
                const kind = assignment.kind
                return (
                  <li key={role} className="binding-role">
                    <header>
                      <strong>{role}</strong>
                      <span className="pill">{kind}</span>
                    </header>
                    <RoleDraftRow
                      role={role}
                      kind={kind}
                      draft={selections[role] ?? { kind: null }}
                      groups={
                        kind === 'agent'
                          ? resources.groups.agent
                          : resources.groups.llm
                      }
                      harnesses={resources.harnesses}
                      connections={resources.connections}
                      onChange={(draft) =>
                        setSelections((prev) => ({ ...prev, [role]: draft }))
                      }
                    />
                  </li>
                )
              })}
            </ol>
          )}
        </section>
        {warnings.length > 0 ? (
          <section className="binding-warnings" aria-label="Подсказки">
            <header>
              <span>Подсказки (не блокируют)</span>
            </header>
            <ul>
              {warnings.map((warning) => (
                <li key={warning.code}>
                  <strong>{formatWarningCode(warning.code)}</strong>
                  <span>{warning.message}</span>
                </li>
              ))}
            </ul>
          </section>
        ) : null}
        <section
          className="binding-provenance"
          aria-label="Происхождение итоговых настроек"
        >
          <header>
            <span>Происхождение итоговых настроек</span>
          </header>
          <p className="hint">
            Значения сохранённой привязки. Черновик формы применяется после
            сохранения.
          </p>
          {resolved.data ? (
            <>
              <p className="meta">
                <span>
                  Схема {resolved.data.schema_version} · execution_hash{' '}
                  {shortHash(resolved.data.execution_hash)} · policy_hash{' '}
                  {shortHash(resolved.data.policy_hash)}
                </span>
              </p>
              <table className="binding-provenance-table">
                <thead>
                  <tr>
                    <th>Имя</th>
                    <th>Источник</th>
                    <th>Значение</th>
                  </tr>
                </thead>
                <tbody>
                  {(resolved.data.provenance ?? []).map((item) => (
                    <tr key={item.name}>
                      <td>
                        <code>{item.name}</code>
                      </td>
                      <td>{formatSettingSource(item.source)}</td>
                      <td>
                        <code>{formatValue(item.value)}</code>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          ) : resolved.error ? (
            <p role="alert" className="error">
              {renderBindingError(resolved.error)}. Исправьте выбор исполнителя
              и сохраните привязку.
            </p>
          ) : (
            <p role="status">Загружаем происхождение…</p>
          )}
        </section>
        <EditorError error={save.error ?? archive.error} onReload={onReload} />
        <section aria-label="Проверка готовности">
          <button
            type="button"
            className="quiet"
            onClick={() => preflight.mutate()}
            disabled={!csrf || preflight.isPending}
          >
            Проверить сохранённую привязку
          </button>
          <p className="hint">
            Проверка использует входы шаблона и сохранённые настройки. Текущие
            правки формы сначала сохраните.
          </p>
          {preflight.error ? (
            <p role="alert" className="error">
              {renderBindingError(preflight.error)}
            </p>
          ) : null}
          {preflight.data ? (
            <>
              <p role="status">
                {preflight.data.ok
                  ? 'Проверка пройдена. Доступность исполнителей повторно проверяется при запуске.'
                  : 'Привязка не готова к запуску.'}
              </p>
              <ul>
                {preflight.data.errors.map((issue, index) => (
                  <li key={index} className="error">
                    {issue.code}: {issue.message} {issue.node_id}{' '}
                    {JSON.stringify(issue.details ?? {})}
                  </li>
                ))}
                {preflight.data.warnings.map((issue, index) => (
                  <li key={`warning-${index}`}>
                    {issue.code}: {issue.message}
                  </li>
                ))}
              </ul>
            </>
          ) : null}
        </section>
        <footer>
          <button
            type="button"
            className="quiet danger"
            onClick={() => archive.mutate()}
            disabled={
              archive.isPending || save.isPending || binding.archived || !csrf
            }
          >
            {archive.isPending ? 'Архивируем…' : 'Архивировать'}
          </button>
          <span className="muted">
            Обновлена {formatDateTime(binding.updated_at)}
          </span>
          <button
            type="button"
            onClick={() => save.mutate()}
            disabled={
              save.isPending ||
              archive.isPending ||
              !csrf ||
              !name.trim() ||
              !version.data
            }
          >
            {save.isPending ? 'Сохраняем…' : 'Сохранить'}
          </button>
        </footer>
      </div>
    </Modal>
  )
}

interface RoleDraftRowProps {
  role: string
  kind: 'agent' | 'llm'
  draft: BindingSelectionDraft
  groups: BindingEditorResources['groups']['agent']
  harnesses: BindingEditorResources['harnesses']
  connections: BindingEditorResources['connections']
  onChange: (draft: BindingSelectionDraft) => void
}

function RoleDraftRow({
  role,
  kind,
  draft,
  groups,
  harnesses,
  connections,
  onChange,
}: RoleDraftRowProps) {
  const selectionKind = draft.kind

  const resourceOptions = useMemo(() => {
    if (kind === 'agent') {
      return harnesses
    }
    return connections
  }, [kind, harnesses, connections])

  const updateDirect = (patch: Partial<BindingSelectionDraft>) => {
    onChange({
      ...draft,
      ...patch,
      kind: 'direct',
    })
  }
  const updateGroup = (groupId: string) => {
    onChange({ kind: 'group', group_id: groupId })
  }
  return (
    <div className="binding-role-row">
      <div
        className="group-toggle"
        role="tablist"
        aria-label={`Тип выбора для ${role}`}
      >
        <button
          type="button"
          role="tab"
          aria-selected={selectionKind === null}
          className="quiet tab"
          onClick={() => onChange({ kind: null })}
        >
          Наследовать
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={selectionKind === 'direct'}
          className={`quiet tab${selectionKind === 'direct' ? ' selected' : ''}`}
          onClick={() => {
            if (selectionKind === 'direct') return
            onChange({ kind: 'direct', model_id: '' })
          }}
        >
          Прямая модель
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={selectionKind === 'group'}
          className={`quiet tab${selectionKind === 'group' ? ' selected' : ''}`}
          onClick={() => {
            if (selectionKind === 'group') return
            onChange({ kind: 'group', group_id: '' })
          }}
        >
          Группа
        </button>
      </div>
      {selectionKind === null ? (
        <p className="hint">
          Выбор из шаблона или узла. Сохранённые значения показаны в таблице
          происхождения. Для старых привязок без отдельного выбора модели
          сохраняются прежние назначения.
        </p>
      ) : selectionKind === 'group' ? (
        <label htmlFor={`role-${role}-group`}>Группа моделей</label>
      ) : (
        <>
          <label htmlFor={`role-${role}-resource`}>
            {kind === 'agent' ? 'Harness-профиль' : 'LLM-подключение'}
          </label>
          <select
            id={`role-${role}-resource`}
            value={
              kind === 'agent'
                ? (draft.harness_profile_id ?? '')
                : (draft.provider_connection_id ?? '')
            }
            onChange={(event) => {
              if (kind === 'agent') {
                updateDirect({
                  harness_profile_id: event.target.value,
                  provider_connection_id: undefined,
                })
              } else {
                updateDirect({
                  provider_connection_id: event.target.value,
                  harness_profile_id: undefined,
                })
              }
            }}
          >
            <option value="" disabled>
              {resourceOptions.length === 0
                ? 'Нет доступных ресурсов'
                : 'Выберите ресурс'}
            </option>
            {resourceOptions.map((option) => (
              <option
                key={option.id}
                value={option.id}
                disabled={option.archived}
              >
                {option.name}
                {option.archived ? ' · архив' : ''}
              </option>
            ))}
          </select>
          <label htmlFor={`role-${role}-model`}>ID модели</label>
          <input
            id={`role-${role}-model`}
            value={draft.model_id ?? ''}
            onChange={(event) => updateDirect({ model_id: event.target.value })}
            list={kind === 'agent' ? undefined : `role-${role}-model-options`}
            spellCheck={false}
          />
          {kind === 'llm' ? (
            <datalist id={`role-${role}-model-options`}>
              {modelSuggestions(draft.provider_connection_id, connections).map(
                (model) => (
                  <option key={model} value={model} />
                ),
              )}
            </datalist>
          ) : null}
        </>
      )}
      {selectionKind === 'group' ? (
        <select
          id={`role-${role}-group`}
          value={draft.group_id ?? ''}
          onChange={(event) => updateGroup(event.target.value)}
        >
          <option value="" disabled>
            {groups.length === 0 ? 'Нет доступных групп' : 'Выберите группу'}
          </option>
          {groups.map((group) => (
            <option key={group.id} value={group.id} disabled={group.archived}>
              {group.name}
              {group.archived ? ' · архив' : ''} · {group.members.length}{' '}
              кандидатов
            </option>
          ))}
        </select>
      ) : null}
      {selectionKind === 'group' && draft.group_id ? (
        <span className="muted">
          {groups
            .find((group) => group.id === draft.group_id)
            ?.members.filter((member) => member.enabled)
            .map((member) => member.model_id)
            .join(', ') || 'нет включённых кандидатов'}
        </span>
      ) : null}
    </div>
  )
}

function initialSelections(
  binding: PipelineBinding | null,
): Record<string, BindingSelectionDraft> {
  if (!binding) return {}
  const entries: Record<string, BindingSelectionDraft> = {}
  for (const [role, selection] of Object.entries(
    binding.model_selections ?? {},
  )) {
    entries[role] = buildDraftFromSelection(selection)
  }
  return entries
}

function modelSuggestions(
  connectionId: string | undefined,
  connections: BindingEditorResources['connections'],
): string[] {
  if (!connectionId) return []
  const connection = connections.find((item) => item.id === connectionId)
  if (!connection) return []
  const seen = new Set<string>()
  const result: string[] = []
  for (const value of [
    ...(connection.manual_models ?? []),
    ...(connection.catalog_models ?? []),
  ]) {
    if (value && !seen.has(value)) {
      seen.add(value)
      result.push(value)
    }
  }
  return result
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'string') return value || '—'
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value)
  }
  try {
    const encoded = JSON.stringify(value)
    return encoded.length > 120 ? `${encoded.slice(0, 117)}…` : encoded
  } catch {
    return '…'
  }
}

function renderBindingError(error: unknown): string {
  if (error instanceof ApiError) return error.body.message
  if (error instanceof Error) return error.message
  return 'Неизвестная ошибка'
}
