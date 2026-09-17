import { EditorError } from './EditorError'
import { MemberParamsEditor, type ParamDraft } from './MemberParamsEditor'
import { ModelScheduleEditor, type ModelSchedule } from './ModelScheduleEditor'
import { validateParamValue } from './model_params'
import { Modal } from '../../app/Modal'
import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError } from '../../api/client'
import { groupsApi, harnessApi, connectionsApi } from '../../api/settings'
import type { ModelGroup } from '../../api/settings'
import { formatDateTime } from '../../app/format'
import { useCsrfToken } from '../../app/session'
import { HarnessModelSelect } from './HarnessModelSelect'
import { useInstalledHarnesses } from './harness_queries'

type GroupKind = 'agent' | 'llm'

export function ModelGroupsPanel() {
  const client = useQueryClient()
  const [kind, setKind] = useState<GroupKind>('agent')
  const [createOpen, setCreateOpen] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const groups = useQuery({
    queryKey: ['model_groups', { kind, includeArchived: false }],
    queryFn: () => groupsApi.list({ kind, includeArchived: false }),
  })
  const sources = useQuery({
    queryKey:
      kind === 'agent'
        ? ['harness_profiles', { includeArchived: true }]
        : ['connections', { includeArchived: true }],
    queryFn: async () =>
      kind === 'agent'
        ? harnessApi.list({ includeArchived: true })
        : connectionsApi.list({ includeArchived: true }),
  })

  return (
    <section className="panel" aria-labelledby="groups-heading">
      <header className="panel-header">
        <span className="section-label" id="groups-heading">
          Группы моделей
        </span>
        <div className="group-toggle" role="tablist" aria-label="Тип группы">
          <button
            type="button"
            role="tab"
            aria-selected={kind === 'agent'}
            className={`quiet tab${kind === 'agent' ? ' selected' : ''}`}
            onClick={() => setKind('agent')}
          >
            agent
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={kind === 'llm'}
            className={`quiet tab${kind === 'llm' ? ' selected' : ''}`}
            onClick={() => setKind('llm')}
          >
            llm
          </button>
        </div>
        <button
          type="button"
          className="quiet"
          onClick={() => setCreateOpen(true)}
        >
          + Новая
        </button>
      </header>
      <GroupList
        sourceNames={Object.fromEntries(
          (sources.data ?? []).map((source) => [
            source.id,
            `${'harness_kind' in source ? (source.harness_kind === 'codex' ? 'Codex' : 'OpenCode') : source.name}${source.archived ? ' · архив' : ''}`,
          ]),
        )}
        groups={groups.data ?? []}
        loading={groups.isLoading}
        error={groups.error ? describeGroupError(groups.error) : null}
        onRetry={() => void groups.refetch()}
        onEdit={(group) => setEditingId(group.id)}
      />
      {createOpen ? (
        <CreateGroupDialog
          kind={kind}
          onClose={() => setCreateOpen(false)}
          onCreated={() => {
            setCreateOpen(false)
            void client.invalidateQueries({ queryKey: ['model_groups'] })
          }}
        />
      ) : null}
      {editingId ? (
        <EditGroupDialog
          groupId={editingId}
          onClose={() => setEditingId(null)}
          onSaved={() => {
            setEditingId(null)
            void client.invalidateQueries({ queryKey: ['model_groups'] })
          }}
        />
      ) : null}
    </section>
  )
}

interface GroupListProps {
  sourceNames: Record<string, string>
  groups: ModelGroup[]
  loading: boolean
  error: string | null
  onRetry: () => void
  onEdit: (group: ModelGroup) => void
}

function GroupList({
  sourceNames,
  groups,
  loading,
  error,
  onRetry,
  onEdit,
}: GroupListProps) {
  if (loading) {
    return (
      <p className="panel-empty" role="status">
        Загружаем группы…
      </p>
    )
  }
  if (error) {
    return (
      <div className="panel-empty">
        <p className="error">{error}</p>
        <button type="button" className="quiet" onClick={onRetry}>
          Повторить
        </button>
      </div>
    )
  }
  if (groups.length === 0) {
    return (
      <p className="panel-empty">
        Групп этого типа пока нет. Создайте первую группу, чтобы настроить
        приоритеты моделей.
      </p>
    )
  }
  return (
    <ul className="panel-list" aria-label="Группы моделей">
      {groups.map((group) => (
        <li key={group.id} className="profile-item">
          <header>
            <strong>{group.name}</strong>
            <span className="pill">{group.kind.toUpperCase()}</span>
          </header>
          {group.description ? (
            <span className="muted">{group.description}</span>
          ) : null}
          <ol className="member-list">
            {group.members.map((member) => (
              <li
                key={member.id}
                className={`member${member.enabled ? '' : ' disabled'}`}
              >
                <span className="member-index">{member.member_index + 1}</span>
                <span className="member-model">
                  {member.model_id}
                  <br />
                  <span className="muted">
                    {sourceNames[
                      member.harness_profile_id ??
                        member.provider_connection_id ??
                        ''
                    ] ??
                      member.harness_profile_id ??
                      member.provider_connection_id}
                  </span>
                </span>
                {!member.enabled ? (
                  <span className="muted">отключён</span>
                ) : null}
                {member.schedule?.enabled ? (
                  <span className="pill">по расписанию</span>
                ) : null}
              </li>
            ))}
          </ol>
          <span className="meta">
            <span>
              Ревизия {group.revision} · обновлена{' '}
              {formatDateTime(group.updated_at)}
            </span>
            <button
              type="button"
              className="quiet"
              onClick={() => onEdit(group)}
            >
              Параметры…
            </button>
          </span>
        </li>
      ))}
    </ul>
  )
}

interface CreateGroupDialogProps {
  kind: GroupKind
  onClose: () => void
  onCreated: () => void
}

function CreateGroupDialog({
  kind,
  onClose,
  onCreated,
}: CreateGroupDialogProps) {
  const csrf = useCsrfToken()
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const harnesses = useInstalledHarnesses(kind === 'agent')
  const connections = useQuery({
    queryKey: ['connections', { includeArchived: false }],
    queryFn: () => connectionsApi.list({ includeArchived: false }),
    enabled: kind === 'llm',
  })
  const [profileId, setProfileId] = useState('')
  const [connectionId, setConnectionId] = useState('')
  const [modelId, setModelId] = useState('')
  const [modelValid, setModelValid] = useState(false)
  const [schedule, setSchedule] = useState<ModelSchedule | null>(null)
  const createAgent = useMutation({
    mutationFn: () =>
      groupsApi.createAgent(
        {
          name: name.trim(),
          description: description.trim() || undefined,
          members: [
            {
              enabled: true,
              harness_profile_id: profileId,
              model_id: modelId.trim(),
              schedule,
            },
          ],
        },
        csrf,
      ),
    onSuccess: onCreated,
  })
  const createLLM = useMutation({
    mutationFn: () =>
      groupsApi.createLLM(
        {
          name: name.trim(),
          description: description.trim() || undefined,
          members: [
            {
              enabled: true,
              provider_connection_id: connectionId,
              model_id: modelId.trim(),
              schedule,
            },
          ],
        },
        csrf,
      ),
    onSuccess: onCreated,
  })
  const create = kind === 'agent' ? createAgent : createLLM
  const ready = useMemo(() => {
    if (!csrf) return false
    if (!name.trim() || !modelId.trim()) return false
    if (kind === 'agent') return Boolean(profileId) && modelValid
    return Boolean(connectionId)
  }, [csrf, name, modelId, kind, profileId, connectionId, modelValid])

  return (
    <Modal
      onClose={onClose}
      busy={create.isPending}
      labelledBy="group-create-title"
    >
      <form
        className="dialog"
        onSubmit={(event) => {
          event.preventDefault()
          if (ready) create.mutate()
        }}
      >
        <header>
          <h3 id="group-create-title">Новая группа · {kind}</h3>
          <button type="button" className="quiet" onClick={onClose}>
            ×
          </button>
        </header>
        <label htmlFor="group-name">Название</label>
        <input
          id="group-name"
          required
          maxLength={128}
          autoFocus
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
        <label htmlFor="group-description">Описание</label>
        <input
          id="group-description"
          value={description}
          onChange={(event) => setDescription(event.target.value)}
        />
        {kind === 'agent' ? (
          <>
            <label htmlFor="group-harness">Harness</label>
            <select
              id="group-harness"
              required
              value={profileId}
              onChange={(event) => {
                setProfileId(event.target.value)
                setModelId('')
                setModelValid(false)
              }}
            >
              <option value="" disabled>
                Выберите harness
              </option>
              {(harnesses.data ?? []).map((profile) => (
                <option key={profile.id} value={profile.id}>
                  {profile.harness_kind === 'codex' ? 'Codex' : 'OpenCode'}
                </option>
              ))}
            </select>
          </>
        ) : (
          <>
            <label htmlFor="group-connection">LLM-подключение</label>
            <select
              id="group-connection"
              required
              value={connectionId}
              onChange={(event) => setConnectionId(event.target.value)}
            >
              <option value="" disabled>
                Выберите подключение
              </option>
              {(connections.data ?? []).map((connection) => (
                <option key={connection.id} value={connection.id}>
                  {connection.name}
                </option>
              ))}
            </select>
          </>
        )}
        <label htmlFor="group-model">
          {kind === 'agent' ? 'Модель harness' : 'ID модели'}
        </label>
        {kind === 'agent' ? (
          <HarnessModelSelect
            id="group-model"
            harness={harnesses.data?.find((h) => h.id === profileId)}
            value={modelId}
            onChange={setModelId}
            onValidityChange={setModelValid}
          />
        ) : (
          <>
            <input
              id="group-model"
              list="group-model-options"
              required
              value={modelId}
              onChange={(event) => setModelId(event.target.value)}
              spellCheck={false}
              placeholder="например, gpt-4.1 или minimax-coding-plan/MiniMax-M3"
            />
            <datalist id="group-model-options">
              {[
                ...new Set([
                  ...(connections.data?.find(
                    (connection) => connection.id === connectionId,
                  )?.catalog_models ?? []),
                  ...(connections.data?.find(
                    (connection) => connection.id === connectionId,
                  )?.manual_models ?? []),
                ]),
              ].map((model) => (
                <option key={model} value={model} />
              ))}
            </datalist>
          </>
        )}
        <p className="hint">
          {kind === 'agent'
            ? 'Выберите harness и модель из её каталога. Одну harness можно добавить несколько раз с разными моделями.'
            : 'Укажите точный ID модели или выберите подсказку из каталога.'}{' '}
          Порядок, состав и параметры кандидатов можно изменить после создания
          кнопкой «Параметры…».
        </p>
        <ModelScheduleEditor value={schedule} onChange={setSchedule} />
        {(kind === 'agent' ? harnesses.isLoading : connections.isLoading) ? (
          <p role="status">Загружаем источники моделей…</p>
        ) : null}
        {(kind === 'agent' ? harnesses.error : connections.error) ? (
          <p className="error" role="alert">
            {describeGroupError(
              kind === 'agent' ? harnesses.error : connections.error,
            )}
          </p>
        ) : null}
        {(
          kind === 'agent'
            ? harnesses.data?.length === 0
            : connections.data?.length === 0
        ) ? (
          <p className="hint">
            {kind === 'agent'
              ? 'Установите Codex или OpenCode и обновите список harness в настройках.'
              : 'Сначала создайте LLM-подключение.'}
          </p>
        ) : null}
        {create.error ? (
          <p className="error" role="alert">
            {create.error instanceof ApiError
              ? create.error.body.message
              : describeGroupError(create.error)}
          </p>
        ) : null}
        <footer>
          <button type="button" className="quiet" onClick={onClose}>
            Отмена
          </button>
          <button type="submit" disabled={create.isPending || !ready}>
            {create.isPending ? 'Создаём…' : 'Создать'}
          </button>
        </footer>
      </form>
    </Modal>
  )
}

interface EditGroupDialogProps {
  groupId: string
  onClose: () => void
  onSaved: () => void
}

interface EditableMember {
  key: string
  id: string | null
  enabled: boolean
  modelId: string
  profileId: string
  connectionId: string
  params: Record<string, unknown>
  schedule: ModelSchedule | null
}

function EditGroupDialog({ groupId, onClose, onSaved }: EditGroupDialogProps) {
  const [reloadIndex, setReloadIndex] = useState(0)
  const group = useQuery({
    queryKey: ['model_group', groupId],
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    queryFn: () => groupsApi.get(groupId),
  })

  if (!group.data || !group.isFetchedAfterMount) {
    return (
      <Modal onClose={onClose} busy={false} label="Настройки">
        <div className="dialog">
          <header>
            <h3>Группа моделей</h3>
            <button type="button" className="quiet" onClick={onClose}>
              ×
            </button>
          </header>
          {group.isFetching ? (
            <p role="status">Загружаем группу…</p>
          ) : group.error ? (
            <p className="error">{describeGroupError(group.error)}</p>
          ) : (
            <p>Группа недоступна.</p>
          )}
        </div>
      </Modal>
    )
  }

  return (
    <EditGroupForm
      key={`${group.data.id}:${reloadIndex}`}
      group={group.data}
      onReload={async () => {
        const result = await group.refetch()
        if (result.isSuccess) setReloadIndex((index) => index + 1)
      }}
      onClose={onClose}
      onSaved={onSaved}
    />
  )
}

interface EditGroupFormProps {
  onReload: () => void
  group: ModelGroup
  onClose: () => void
  onSaved: () => void
}

function EditGroupForm({
  group: initial,
  onClose,
  onSaved,
  onReload,
}: EditGroupFormProps) {
  const client = useQueryClient()
  const [group, setGroup] = useState(initial)
  const csrf = useCsrfToken()
  const harnesses = useInstalledHarnesses(group.kind === 'agent')
  const connections = useQuery({
    queryKey: ['connections', { includeArchived: false }],
    queryFn: () => connectionsApi.list({ includeArchived: false }),
    enabled: group.kind === 'llm',
  })
  const [name, setName] = useState(group.name)
  const [description, setDescription] = useState(group.description)
  const [members, setMembers] = useState<EditableMember[]>(() =>
    group.members.map((member) => ({
      key: member.id,
      id: member.id,
      enabled: member.enabled,
      modelId: member.model_id,
      profileId: member.harness_profile_id ?? '',
      connectionId: member.provider_connection_id ?? '',
      params: member.params,
      schedule: member.schedule ?? null,
    })),
  )
  const [expandedParams, setExpandedParams] = useState<Record<string, boolean>>(
    {},
  )
  const [paramDrafts, setParamDrafts] = useState<Record<string, ParamDraft>>({})
  const [modelValidity, setModelValidity] = useState<Record<string, boolean>>(
    {},
  )

  function accept(updated: ModelGroup) {
    setGroup(updated)
    client.setQueryData(['model_group', updated.id], updated)
    void client.invalidateQueries({ queryKey: ['model_groups'] })
  }
  function acceptSaved(updated: ModelGroup) {
    const expanded = Object.fromEntries(
      updated.members.map((member, index) => [
        member.id,
        Boolean(expandedParams[members[index]?.key]),
      ]),
    )
    accept(updated)
    setName(updated.name)
    setDescription(updated.description)
    setExpandedParams(expanded)
    setParamDrafts({})
    setMembers(
      updated.members.map((member) => ({
        key: member.id,
        id: member.id,
        enabled: member.enabled,
        modelId: member.model_id,
        profileId: member.harness_profile_id ?? '',
        connectionId: member.provider_connection_id ?? '',
        params: member.params,
        schedule: member.schedule ?? null,
      })),
    )
  }

  const save = useMutation({
    mutationFn: () => {
      const metadata = {
        expected_revision: group.revision,
        name: name.trim(),
        description: description.trim(),
      }
      const candidates = members.map((member) => ({
        id: member.id,
        enabled: member.enabled,
        model_id: member.modelId.trim(),
        params: member.params,
        schedule: member.schedule,
      }))
      return group.kind === 'agent'
        ? groupsApi.updateAgent(
            group.id,
            {
              ...metadata,
              members: candidates.map((member, index) => ({
                ...member,
                harness_profile_id: members[index].profileId,
              })),
            },
            csrf,
          )
        : groupsApi.updateLLM(
            group.id,
            {
              ...metadata,
              members: candidates.map((member, index) => ({
                ...member,
                provider_connection_id: members[index].connectionId,
              })),
            },
            csrf,
          )
    },
    onSuccess: acceptSaved,
  })
  const archive = useMutation({
    mutationFn: () => groupsApi.archive(group.id, group.revision, csrf),
    onSuccess: (updated) => {
      accept(updated)
      onSaved()
    },
  })
  const copyGroup = useMutation({
    mutationFn: () =>
      groupsApi.copy(
        group.id,
        {
          expected_revision: group.revision,
          name: `${name.trim().slice(0, 112)} - копия`,
          description: group.description,
        },
        csrf,
      ),
    onSuccess: onSaved,
  })

  const kind = group.kind
  const paramsValid = members.every((member) =>
    Object.entries(member.params).every(([name, value]) => {
      const draft = paramDrafts[`${member.key}:${name}`]
      if (draft?.error) return false
      return validateParamValue(name, value) === null
    }),
  )
  const validMembers =
    members.some((member) => member.enabled) &&
    members.every(
      (member) =>
        member.modelId.trim() &&
        (kind === 'agent'
          ? member.profileId &&
            (modelValidity[member.key] ||
              group.members.some(
                (original) =>
                  original.id === member.id &&
                  original.harness_profile_id === member.profileId &&
                  original.model_id === member.modelId,
              ))
          : member.connectionId),
    ) &&
    paramsValid
  const metadataDirty =
    name.trim() !== group.name || description.trim() !== group.description
  const membersDirty =
    !paramsValid ||
    JSON.stringify(
      members.map(
        ({
          id,
          enabled,
          modelId,
          profileId,
          connectionId,
          params,
          schedule,
        }) => ({
          id,
          enabled,
          modelId,
          profileId,
          connectionId,
          params,
          schedule,
        }),
      ),
    ) !==
      JSON.stringify(
        group.members.map((member) => ({
          id: member.id,
          enabled: member.enabled,
          modelId: member.model_id,
          profileId: member.harness_profile_id ?? '',
          connectionId: member.provider_connection_id ?? '',
          params: member.params,
          schedule: member.schedule ?? null,
        })),
      )

  function updateMember(index: number, patch: Partial<EditableMember>) {
    setMembers((prev) =>
      prev.map((member, i) => (i === index ? { ...member, ...patch } : member)),
    )
  }

  function moveMember(index: number, direction: -1 | 1) {
    setMembers((prev) => {
      const target = index + direction
      if (target < 0 || target >= prev.length) return prev
      const next = [...prev]
      const [removed] = next.splice(index, 1)
      next.splice(target, 0, removed)
      return next
    })
  }

  function removeMember(index: number) {
    setMembers((prev) => prev.filter((_, i) => i !== index))
  }

  function setParamDraft(key: string, draft: ParamDraft | null) {
    setParamDrafts((prev) => {
      const next = { ...prev }
      if (draft === null) delete next[key]
      else next[key] = draft
      return next
    })
  }

  function toggleParams(key: string) {
    setExpandedParams((prev) => ({ ...prev, [key]: !prev[key] }))
  }

  function isUnverifiedHarnessProfile(profileId: string): boolean {
    return (
      kind === 'agent' &&
      (harnesses.data ?? []).some(
        (profile) =>
          profile.id === profileId &&
          ['opencode', 'codex'].includes(profile.harness_kind),
      )
    )
  }

  function appendMember() {
    setMembers((prev) => [
      ...prev,
      {
        key: crypto.randomUUID(),
        id: null,
        enabled: true,
        modelId: '',
        profileId: kind === 'agent' ? (harnesses.data?.[0]?.id ?? '') : '',
        connectionId: kind === 'llm' ? (connections.data?.[0]?.id ?? '') : '',
        params: {},
        schedule: null,
      },
    ])
  }

  return (
    <Modal
      onClose={onClose}
      busy={save.isPending || archive.isPending || copyGroup.isPending}
      labelledBy="group-edit-title"
    >
      <div className="dialog wide group-settings-dialog">
        <header>
          <h3 id="group-edit-title">{group.name}</h3>
          <button
            type="button"
            className="quiet"
            aria-label="Закрыть настройки группы"
            onClick={onClose}
          >
            ×
          </button>
        </header>
        <label htmlFor="group-edit-name">Название</label>
        <input
          id="group-edit-name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          maxLength={128}
        />
        <label htmlFor="group-edit-description">Описание</label>
        <input
          id="group-edit-description"
          value={description}
          onChange={(event) => setDescription(event.target.value)}
        />
        <div className="member-editor">
          <header>
            <span>Кандидаты</span>
            <button
              type="button"
              className="quiet"
              onClick={appendMember}
              disabled={
                (kind === 'agent' && !harnesses.data?.length) ||
                (kind === 'llm' && !connections.data?.length)
              }
            >
              + Добавить
            </button>
          </header>
          {members.length === 0 ? (
            <p className="hint">Группа без кандидатов не готова к запуску.</p>
          ) : (
            <ol className="member-edit-list">
              {members.map((member, index) => (
                <li key={member.key}>
                  <div className="member-row">
                    <span className="member-index">{index + 1}</span>
                    {kind === 'agent' ? (
                      <select
                        aria-label="Harness"
                        value={member.profileId}
                        onChange={(event) =>
                          updateMember(index, {
                            profileId: event.target.value,
                            modelId: '',
                            params: {},
                          })
                        }
                      >
                        {!harnesses.data?.some(
                          (profile) => profile.id === member.profileId,
                        ) ? (
                          <option value={member.profileId}>
                            Недоступен ·{' '}
                            {member.profileId || 'выберите harness'}
                          </option>
                        ) : null}
                        {(harnesses.data ?? []).map((profile) => (
                          <option key={profile.id} value={profile.id}>
                            {profile.harness_kind === 'codex'
                              ? 'Codex'
                              : 'OpenCode'}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <select
                        aria-label="LLM-подключение"
                        value={member.connectionId}
                        onChange={(event) =>
                          updateMember(index, {
                            connectionId: event.target.value,
                          })
                        }
                      >
                        {!connections.data?.some(
                          (connection) => connection.id === member.connectionId,
                        ) ? (
                          <option value={member.connectionId}>
                            Недоступно ·{' '}
                            {member.connectionId || 'выберите подключение'}
                          </option>
                        ) : null}
                        {(connections.data ?? []).map((connection) => (
                          <option key={connection.id} value={connection.id}>
                            {connection.name}
                          </option>
                        ))}
                      </select>
                    )}
                    {kind === 'agent' ? (
                      <HarnessModelSelect
                        harness={harnesses.data?.find(
                          (h) => h.id === member.profileId,
                        )}
                        value={member.modelId}
                        onChange={(modelId) =>
                          updateMember(index, { modelId, params: {} })
                        }
                        onValidityChange={(valid) =>
                          setModelValidity((previous) =>
                            previous[member.key] === valid
                              ? previous
                              : { ...previous, [member.key]: valid },
                          )
                        }
                      />
                    ) : (
                      <input
                        aria-label="ID модели"
                        value={member.modelId}
                        spellCheck={false}
                        onChange={(event) =>
                          updateMember(index, { modelId: event.target.value })
                        }
                      />
                    )}
                    <label className="enabled-toggle">
                      <input
                        type="checkbox"
                        checked={member.enabled}
                        onChange={(event) =>
                          updateMember(index, { enabled: event.target.checked })
                        }
                      />
                      активен
                    </label>
                    <div className="member-actions">
                      <button
                        type="button"
                        className="quiet"
                        aria-expanded={Boolean(expandedParams[member.key])}
                        onClick={() => toggleParams(member.key)}
                      >
                        Параметры
                        {Object.keys(member.params).length > 0
                          ? ` (${Object.keys(member.params).length})`
                          : ''}
                        {member.schedule?.enabled ? ' · расписание' : ''}
                      </button>
                      <button
                        type="button"
                        className="quiet"
                        aria-label="Выше"
                        onClick={() => moveMember(index, -1)}
                        disabled={index === 0}
                      >
                        ↑
                      </button>
                      <button
                        type="button"
                        className="quiet"
                        aria-label="Ниже"
                        onClick={() => moveMember(index, 1)}
                        disabled={index === members.length - 1}
                      >
                        ↓
                      </button>
                      <button
                        type="button"
                        className="quiet danger"
                        onClick={() => removeMember(index)}
                      >
                        Удалить
                      </button>
                    </div>
                  </div>
                  {expandedParams[member.key] ? (
                    <>
                      <MemberParamsEditor
                        memberKey={member.key}
                        params={member.params}
                        drafts={paramDrafts}
                        supportedReasoningEfforts={(() => {
                          const metadata = harnesses.data?.find(
                            (profile) => profile.id === member.profileId,
                          )?.model_capabilities?.[member.modelId]
                          return Array.isArray(metadata?.reasoning_efforts)
                            ? metadata.reasoning_efforts.filter(
                                (value): value is string =>
                                  typeof value === 'string',
                              )
                            : undefined
                        })()}
                        harnessHint={
                          Object.keys(member.params).length > 0 &&
                          isUnverifiedHarnessProfile(member.profileId) &&
                          !harnesses.data?.find(
                            (profile) => profile.id === member.profileId,
                          )?.model_capabilities?.[member.modelId]
                        }
                        onParams={(next, removed) => {
                          updateMember(index, { params: next })
                          if (removed) {
                            setParamDraft(`${member.key}:${removed}`, null)
                          }
                        }}
                        onDraft={setParamDraft}
                      />
                      <ModelScheduleEditor
                        value={member.schedule}
                        onChange={(schedule) =>
                          updateMember(index, { schedule })
                        }
                      />
                    </>
                  ) : null}
                </li>
              ))}
            </ol>
          )}
        </div>
        <p className="hint">
          «Сохранить» сохраняет название, описание, состав, порядок, параметры и
          расписания. Для копирования сначала сохраните правки. Параметры
          агентов выбираются из актуального каталога harness и повторно
          проверяются перед запуском. Для LLM сервер проверяет общий набор и
          ограничения адаптера. Изменения группы применятся только к новым Run.
        </p>
        {!paramsValid ? (
          <p className="error" role="alert">
            Исправьте параметры кандидатов перед сохранением.
          </p>
        ) : null}
        {[save.error, archive.error, copyGroup.error].map((error, index) => (
          <EditorError key={index} error={error} onReload={onReload} />
        ))}
        {(kind === 'agent' ? harnesses.error : connections.error) ? (
          <p className="error" role="alert">
            {describeGroupError(
              kind === 'agent' ? harnesses.error : connections.error,
            )}
          </p>
        ) : null}
        {save.isSuccess && !metadataDirty && !membersDirty ? (
          <p role="status" className="hint">
            Группа сохранена.
          </p>
        ) : null}
        <footer>
          <button
            type="button"
            className="quiet danger"
            onClick={() => archive.mutate()}
            disabled={archive.isPending || !csrf}
          >
            {archive.isPending ? 'Архивируем…' : 'Архивировать'}
          </button>
          <button
            type="button"
            className="quiet"
            onClick={() => copyGroup.mutate()}
            disabled={
              copyGroup.isPending || !csrf || metadataDirty || membersDirty
            }
          >
            {copyGroup.isPending ? 'Копируем…' : 'Копировать'}
          </button>
          <button
            type="button"
            onClick={() => save.mutate()}
            disabled={save.isPending || !csrf || !name.trim() || !validMembers}
          >
            {save.isPending ? 'Сохраняем…' : 'Сохранить'}
          </button>
        </footer>
      </div>
    </Modal>
  )
}

function describeGroupError(error: unknown): string {
  if (error instanceof ApiError) return error.body.message
  if (error instanceof Error) return error.message
  return 'Неизвестная ошибка'
}
