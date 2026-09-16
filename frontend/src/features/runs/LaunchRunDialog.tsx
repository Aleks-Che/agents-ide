import { useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError } from '../../api/client'
import type { ApiSchemas } from '../../api/generated'
import {
  bindingsApi,
  templatesApi,
  type PipelineBinding,
  type PreflightReport,
  type ResolvedSettings,
} from '../../api/bindings'
import { connectionsApi, groupsApi, harnessApi } from '../../api/settings'
import { runsApi, type RunRecord } from '../../api/runs'
import type { Chat, Project } from '../../api/projects'
import { Modal } from '../../app/Modal'
import { formatDateTime, shortHash } from '../../app/format'
import { useCsrfToken } from '../../app/session'
import {
  formatSettingSource,
  type BindingSelectionDraft,
} from '../bindings/utils'
import {
  launchParameters,
  launchError,
  loadPendingStart,
  pendingStartKey,
  uncertainStart,
  type StartRequest,
} from './launch'
import {
  buildSingleAgent,
  singleAgentNodes,
  type SingleAgentNode,
  type LaunchResources,
} from './launch_single_agent'

type LaunchMode = 'binding' | 'single_agent'

interface LaunchRunDialogProps {
  planningSource?: ApiSchemas['PlanningSource'] | null
  project: Project
  chat: Chat
  draft: string
  bindings: PipelineBinding[]
  bindingsLoading: boolean
  bindingsError: unknown
  onRetryBindings: () => void
  onClose: () => void
  onLaunched: (run: RunRecord) => void
}

export function LaunchRunDialog({
  planningSource,
  project,
  chat,
  draft,
  bindings,
  bindingsLoading,
  bindingsError,
  onRetryBindings,
  onClose,
  onLaunched,
}: LaunchRunDialogProps) {
  const csrf = useCsrfToken()
  const client = useQueryClient()
  const [mode, setMode] = useState<LaunchMode>('binding')
  const [bindingId, setBindingId] = useState('')
  const [executionMode, setExecutionMode] = useState<'real' | 'simulated'>(
    'real',
  )
  const [useDraft, setUseDraft] = useState(false)
  const [inputsText, setInputsText] = useState('{}')
  const [limitsText, setLimitsText] = useState('{}')
  const [commandsText, setCommandsText] = useState('null')
  const [singleNodeId, setSingleNodeId] = useState('')
  const [singleSelection, setSingleSelection] = useState<BindingSelectionDraft>(
    { kind: null },
  )
  const [singleParametersText, setSingleParametersText] = useState('{}')
  const [trustedKey, setTrustedKey] = useState<string | null>(null)
  const [storage] = useState(() => {
    try {
      return { body: loadPendingStart(project.id, chat.id), error: null }
    } catch {
      return {
        body: null,
        error:
          'Не удалось прочитать сохранённый запрос запуска. Восстановите доступ к хранилищу браузера и откройте окно снова.',
      }
    }
  })
  const [pending, setPending] = useState<StartRequest | null>(storage.body)
  const sending = useRef(false)
  const selectedBinding = bindings.find(
    (binding) => binding.id === bindingId && !binding.archived,
  )
  const version = useQuery({
    queryKey: ['version', selectedBinding?.version_id],
    queryFn: () => templatesApi.getVersion(selectedBinding!.version_id),
    enabled: Boolean(selectedBinding),
  })
  const resourceState = useLaunchResources(mode === 'single_agent')
  const resources = resourceState.resources
  const nodes = useMemo(
    () => (version.data ? singleAgentNodes(version.data.graph) : []),
    [version.data],
  )
  const selectedNode = nodes.find((node) => node.id === singleNodeId)

  // Every user-editable launch input invalidates both the preview and import consent.
  const formKey = JSON.stringify([
    planningSource,
    mode,
    bindingId,
    selectedBinding?.version,
    executionMode,
    inputsText,
    limitsText,
    commandsText,
    useDraft,
    draft,
    singleNodeId,
    singleSelection,
    singleParametersText,
  ])
  const check = useMutation({
    mutationFn: async () => {
      if (!selectedBinding) throw new Error('Выберите доступную привязку.')
      const parameters = launchParameters(
        executionMode,
        inputsText,
        limitsText,
        commandsText,
      )
      if (planningSource) parameters.planning_source = planningSource
      const message = useDraft && draft.trim() ? draft.trim() : undefined
      if (message && message.length > 65536)
        throw new Error('Черновик превышает 65536 символов.')
      let singleAgent: ApiSchemas['SingleAgentSpec'] | undefined
      if (mode === 'single_agent') {
        if (!selectedNode) throw new Error('Выберите узел одиночного агента.')
        singleAgent = buildSingleAgent(
          selectedNode.role,
          singleSelection,
          singleParametersText,
          resources,
          selectedNode,
        )
        parameters.single_agent = singleAgent
      }
      const [report, resolved] = await Promise.all([
        bindingsApi.preflight(bindingId, csrf, parameters),
        singleAgent
          ? Promise.resolve({ data: null, error: null })
          : bindingsApi
              .resolvedWithOverrides(
                bindingId,
                parameters.overrides ?? {},
                csrf,
              )
              .then((data) => ({ data, error: null }))
              .catch((error: unknown) => ({
                data: null,
                error: launchError(error),
              })),
      ])
      return {
        key: formKey,
        report,
        resolved,
        parameters,
        message,
        bindingId,
        singleAgent,
      }
    },
  })
  const checked = check.data?.key === formKey ? check.data : null
  const requiresTrust = checked?.report.preview.requires_trust === true
  const launch = useMutation({
    mutationFn: async (body: StartRequest) => {
      try {
        sessionStorage.setItem(pendingStartKey(chat.id), JSON.stringify(body))
      } catch {
        throw new Error(
          'Не удалось сохранить ключ запуска в браузере. Запрос не отправлен; восстановите хранилище и повторите.',
        )
      }
      setPending(body)
      return runsApi.start(body, csrf)
    },
    onSuccess: (run) => {
      sessionStorage.removeItem(pendingStartKey(chat.id))
      setPending(null)
      void client.invalidateQueries({ queryKey: ['runs_summary'] })
      void client.invalidateQueries({ queryKey: ['runs_for_chat'] })
      onLaunched(run)
    },
    onError: (error) => {
      if (!uncertainStart(error)) {
        sessionStorage.removeItem(pendingStartKey(chat.id))
        setPending(null)
        check.reset()
        setTrustedKey(null)
      }
    },
    onSettled: () => {
      sending.current = false
    },
  })
  const singleAgentReady = mode !== 'single_agent' || Boolean(selectedNode)
  const ready = Boolean(
    checked?.report.ok &&
    checked.report.execution_hash &&
    (!requiresTrust || trustedKey === formKey) &&
    singleAgentReady,
  )
  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    if (sending.current || !csrf || storage.error) return
    let body = pending
    if (!body) {
      if (!ready || !checked || !selectedBinding || bindingsError) return
      body = {
        ...checked.parameters,
        project_id: project.id,
        chat_id: chat.id,
        binding_id: checked.bindingId,
        message: checked.message,
        initiator: 'ui',
        idempotency_key: crypto.randomUUID(),
        trusted_execution_hash: checked.report.execution_hash,
      }
      if (mode === 'single_agent' && checked.singleAgent) {
        body.single_agent = checked.singleAgent
      }
    }
    sending.current = true
    launch.mutate(body)
  }
  return (
    <Modal
      onClose={onClose}
      busy={launch.isPending || check.isPending}
      labelledBy="launch-run-title"
    >
      <form className="dialog wide launch-dialog" onSubmit={submit}>
        <header>
          <h3 id="launch-run-title">Запустить задание</h3>
          <button
            type="button"
            className="quiet"
            aria-label="Закрыть"
            onClick={onClose}
          >
            ×
          </button>
        </header>
        <p className="hint">
          Проект: <strong>{project.name}</strong> · Диалог:{' '}
          <strong>{chat.title}</strong>
        </p>
        {storage.error ? (
          <p role="alert" className="error">
            {storage.error}
          </p>
        ) : null}
        {pending ? (
          <section aria-label="Неподтверждённый запуск">
            <p role="status">
              Ответ на запуск не подтверждён. Повтор отправит тот же запрос и
              вернёт уже созданный Run, если сервер успел его сохранить.
            </p>
            <p>
              Ключ: <code>{pending.idempotency_key}</code> · режим{' '}
              {pending.execution_mode}
              {pending.single_agent ? (
                <>
                  {' '}
                  · одиночный агент <code>{pending.single_agent.role}</code>
                </>
              ) : null}
            </p>
            <details>
              <summary>Сохранённый запрос</summary>
              <pre>{JSON.stringify(pending, null, 2)}</pre>
            </details>
          </section>
        ) : (
          <>
            <fieldset className="mode-toggle">
              <legend>Режим запуска</legend>
              <label>
                <input
                  type="radio"
                  name="launch-mode-kind"
                  checked={mode === 'binding'}
                  onChange={() => {
                    setMode('binding')
                    check.reset()
                    setTrustedKey(null)
                  }}
                />
                Привязка проекта
              </label>
              <label>
                <input
                  type="radio"
                  name="launch-mode-kind"
                  checked={mode === 'single_agent'}
                  onChange={() => {
                    setMode('single_agent')
                    check.reset()
                    setTrustedKey(null)
                  }}
                />
                Одиночный агент
              </label>
            </fieldset>
            {bindingsError ? (
              <p role="alert" className="error">
                Не удалось загрузить привязки: {launchError(bindingsError)}{' '}
                <button type="button" onClick={onRetryBindings}>
                  Повторить загрузку привязок
                </button>
              </p>
            ) : null}
            {bindingsLoading ? (
              <p role="status">Загружаем привязки проекта…</p>
            ) : null}
            <label htmlFor="launch-binding">Привязка</label>
            <select
              id="launch-binding"
              value={bindingId}
              onChange={(event) => {
                setBindingId(event.target.value)
                setSingleNodeId('')
                setSingleSelection({ kind: null })
                setSingleParametersText('{}')
                check.reset()
                setTrustedKey(null)
              }}
              disabled={bindingsLoading || Boolean(bindingsError)}
            >
              <option value="">Выберите привязку</option>
              {bindings
                .filter((binding) => !binding.archived)
                .map((binding) => (
                  <option key={binding.id} value={binding.id}>
                    {binding.name}
                  </option>
                ))}
            </select>
            {!bindingsLoading &&
            !bindingsError &&
            !bindings.some((binding) => !binding.archived) ? (
              <p className="hint">
                Нет доступных привязок. Создайте привязку из версии в
                Библиотеке.
              </p>
            ) : null}
            {bindingId && !selectedBinding ? (
              <p role="alert">
                Выбранная привязка недоступна. Выберите другую.
              </p>
            ) : null}
            <fieldset className="mode-toggle">
              <legend>Режим исполнения</legend>
              <label>
                <input
                  type="radio"
                  name="launch-execution-mode"
                  checked={executionMode === 'real'}
                  onChange={() => setExecutionMode('real')}
                />
                Реальный
              </label>
              <label>
                <input
                  type="radio"
                  name="launch-execution-mode"
                  checked={executionMode === 'simulated'}
                  onChange={() => setExecutionMode('simulated')}
                />
                Имитация (fake)
              </label>
            </fieldset>
            {version.isLoading ? (
              <p role="status">Загружаем граф версии…</p>
            ) : null}
            {version.error ? (
              <p role="alert" className="error">
                {launchError(version.error)}{' '}
                <button type="button" onClick={() => void version.refetch()}>
                  Повторить загрузку версии
                </button>
              </p>
            ) : null}
            {mode === 'single_agent' && selectedBinding ? (
              <>
                {resourceState.loading ? (
                  <p role="status">Загружаем исполнителей…</p>
                ) : null}
                {resourceState.errors.map((error, index) => (
                  <p role="alert" className="error" key={index}>
                    Не удалось загрузить исполнителей: {launchError(error)}{' '}
                    <button type="button" onClick={resourceState.retry}>
                      Повторить загрузку исполнителей
                    </button>
                  </p>
                ))}
                <SingleAgentForm
                  nodes={nodes}
                  loaded={Boolean(version.data)}
                  nodeId={singleNodeId}
                  onNode={(nodeId) => {
                    setSingleNodeId(nodeId)
                    setSingleSelection({ kind: null })
                    setSingleParametersText('{}')
                    check.reset()
                    setTrustedKey(null)
                  }}
                  selection={singleSelection}
                  onSelection={(draft) => {
                    setSingleSelection(draft)
                    check.reset()
                    setTrustedKey(null)
                  }}
                  parametersText={singleParametersText}
                  onParameters={(text) => {
                    setSingleParametersText(text)
                    check.reset()
                    setTrustedKey(null)
                  }}
                  resources={resources}
                />
              </>
            ) : null}
            {version.data ? (
              <details>
                <summary>Граф и настройки версии</summary>
                <pre>
                  {JSON.stringify(
                    {
                      graph: version.data.graph,
                      settings: version.data.settings,
                    },
                    null,
                    2,
                  )}
                </pre>
              </details>
            ) : null}
            {version.data ? (
              <details>
                <summary>Схема входов и значения версии</summary>
                <pre>
                  {JSON.stringify(
                    {
                      input_schema: version.data.graph.input_schema ?? {},
                      defaults: version.data.inputs,
                    },
                    null,
                    2,
                  )}
                </pre>
              </details>
            ) : null}
            {planningSource ? (
              <p className="hint">
                Подтверждённый план Council: {planningSource.job_id.slice(0, 8)}
                , ревизия {planningSource.revision_number}. Текст, ответы и
                критерии будут зафиксированы в Run; их нельзя заменить входами
                ниже.
              </p>
            ) : null}
            <label htmlFor="launch-inputs">Входы запуска (JSON)</label>
            <textarea
              id="launch-inputs"
              rows={6}
              value={inputsText}
              onChange={(event) => setInputsText(event.target.value)}
            />
            <p className="hint">
              Эти поля дополняют и переопределяют входы версии. Сообщения
              диалога и выбранный черновик передаются отдельно; они не заменяют
              обязательные входы графа.
            </p>
            <label>
              <input
                type="checkbox"
                checked={useDraft}
                disabled={!draft.trim()}
                onChange={(event) => setUseDraft(event.target.checked)}
              />
              Включить текущий черновик в запуск
            </label>
            {useDraft ? (
              <pre aria-label="Черновик для запуска">{draft}</pre>
            ) : null}
            <p className="hint">
              Сохранённые сообщения диалога войдут в снимок на момент запуска.
              Черновик не публикуется в ленту и остаётся в редакторе.
            </p>
            <details>
              <summary>Лимиты и фильтр команд</summary>
              <label htmlFor="launch-limits">Лимиты запуска (JSON)</label>
              <textarea
                id="launch-limits"
                rows={3}
                value={limitsText}
                onChange={(event) => setLimitsText(event.target.value)}
              />
              <p className="hint">
                max_calls, max_node_visits, max_backward_transitions,
                max_duration_seconds. Пустой объект сохраняет лимиты привязки.
              </p>
              <label htmlFor="launch-commands">Фильтр команд (JSON)</label>
              <textarea
                id="launch-commands"
                rows={2}
                value={commandsText}
                onChange={(event) => setCommandsText(event.target.value)}
              />
              <p className="hint">
                null — наследовать; [] — не выполнять команды; массив ID —
                выполнить только выбранные. Обязательные команды проверит
                сервер.
              </p>
            </details>
            <section className="preflight-summary" aria-label="Preflight">
              <header>
                <strong>Проверка перед запуском</strong>
                <button
                  type="button"
                  className="quiet"
                  disabled={
                    !selectedBinding ||
                    !version.data ||
                    Boolean(version.error) ||
                    Boolean(bindingsError) ||
                    !csrf ||
                    (mode === 'single_agent' && !selectedNode)
                  }
                  onClick={() => {
                    setTrustedKey(null)
                    launch.reset()
                    check.mutate()
                  }}
                >
                  {check.isPending ? 'Проверяем…' : 'Запустить preflight'}
                </button>
              </header>
              {check.error ? (
                <p role="alert" className="error">
                  {launchError(check.error)}
                </p>
              ) : null}
              {!checked ? (
                <p className="hint">
                  Проверьте текущие входы и режим перед запуском. Изменение
                  формы требует новой проверки.
                </p>
              ) : (
                <>
                  <LaunchPreview
                    report={checked.report}
                    resolved={checked.resolved.data}
                    singleAgent={checked.singleAgent}
                  />
                  {checked.resolved.error ? (
                    <p className="error">
                      Происхождение ролей: {checked.resolved.error}
                    </p>
                  ) : null}
                  {requiresTrust ? (
                    <label>
                      <input
                        type="checkbox"
                        checked={trustedKey === formKey}
                        onChange={(event) =>
                          setTrustedKey(event.target.checked ? formKey : null)
                        }
                      />
                      Доверяю импортированной конфигурации, входам и назначениям
                      из этой проверки
                    </label>
                  ) : null}
                </>
              )}
            </section>
          </>
        )}
        {launch.error ? (
          <div className="error" role="alert">
            <p>{launchError(launch.error)}</p>
            {launch.error instanceof ApiError ? (
              <pre>{JSON.stringify(launch.error.body.details, null, 2)}</pre>
            ) : null}
          </div>
        ) : null}
        <footer>
          <button type="button" className="quiet" onClick={onClose}>
            Закрыть
          </button>
          <button
            type="submit"
            disabled={
              !csrf ||
              Boolean(storage.error) ||
              (!pending &&
                (!ready ||
                  Boolean(bindingsError) ||
                  !selectedBinding ||
                  (mode === 'single_agent' && !singleAgentReady)))
            }
          >
            {launch.isPending
              ? 'Запускаем…'
              : pending
                ? 'Повторить тот же запуск'
                : 'Запустить'}
          </button>
        </footer>
      </form>
    </Modal>
  )
}

function useLaunchResources(enabled: boolean) {
  const harnesses = useQuery({
    enabled,
    queryKey: ['harness_profiles', { includeArchived: true }],
    queryFn: () => harnessApi.list({ includeArchived: true }),
  })
  const connections = useQuery({
    enabled,
    queryKey: ['connections', { includeArchived: true }],
    queryFn: () => connectionsApi.list({ includeArchived: true }),
  })
  const agentGroups = useQuery({
    enabled,
    queryKey: ['model_groups', { kind: 'agent', includeArchived: true }],
    queryFn: () => groupsApi.list({ kind: 'agent', includeArchived: true }),
  })
  const llmGroups = useQuery({
    enabled,
    queryKey: ['model_groups', { kind: 'llm', includeArchived: true }],
    queryFn: () => groupsApi.list({ kind: 'llm', includeArchived: true }),
  })
  return {
    loading: [harnesses, connections, agentGroups, llmGroups].some(
      (query) => query.isLoading,
    ),
    errors: [harnesses, connections, agentGroups, llmGroups].flatMap((query) =>
      query.error ? [query.error] : [],
    ),
    retry: () => {
      for (const query of [harnesses, connections, agentGroups, llmGroups])
        void query.refetch()
    },
    resources: {
      groups: { agent: agentGroups.data ?? [], llm: llmGroups.data ?? [] },
      harnesses: harnesses.data ?? [],
      connections: connections.data ?? [],
    },
  }
}

interface SingleAgentFormProps {
  nodes: SingleAgentNode[]
  loaded: boolean
  nodeId: string
  onNode: (nodeId: string) => void
  selection: BindingSelectionDraft
  onSelection: (draft: BindingSelectionDraft) => void
  parametersText: string
  onParameters: (text: string) => void
  resources: LaunchResources
}

function SingleAgentForm({
  nodes,
  loaded,
  nodeId,
  onNode,
  selection,
  onSelection,
  parametersText,
  onParameters,
  resources,
}: SingleAgentFormProps) {
  const node = nodes.find((item) => item.id === nodeId)
  return (
    <section className="single-agent-form" aria-label="Одиночный агент">
      <header>
        <strong>Одиночный агент</strong>
      </header>
      {loaded && nodes.length === 0 ? (
        <p className="hint">
          Граф этой версии не объявляет AgentTask/LLMRequest с ролью. Одиночный
          запуск недоступен — используйте запуск привязки.
        </p>
      ) : null}
      <label htmlFor="single-role">Узел и роль</label>
      <select
        id="single-role"
        value={nodeId}
        onChange={(event) => onNode(event.target.value)}
      >
        <option value="">Выберите узел</option>
        {nodes.map((node) => (
          <option key={node.id} value={node.id}>
            {node.role} · {node.kind} · {node.id}
          </option>
        ))}
      </select>
      {node ? (
        <SingleAgentDirectRow
          kind={node.kind}
          draft={selection}
          onChange={onSelection}
          resources={resources}
        />
      ) : null}
      <label htmlFor="single-parameters">Параметры модели (JSON)</label>
      <textarea
        id="single-parameters"
        rows={3}
        value={parametersText}
        onChange={(event) => onParameters(event.target.value)}
      />
      <p className="hint">
        Указанные поля дополняют параметры узла; пустой объект сохраняет
        наследование. Допускаются только поля возможностей модели (temperature,
        top_p и т. п.); credentials, role, prompt, инструменты и адреса
        запрещены и будут отклонены сервером.
      </p>
    </section>
  )
}

interface SingleAgentDirectRowProps {
  kind: 'agent' | 'llm'
  draft: BindingSelectionDraft
  onChange: (draft: BindingSelectionDraft) => void
  resources: LaunchResources
}

function SingleAgentDirectRow({
  kind,
  draft,
  onChange,
  resources,
}: SingleAgentDirectRowProps) {
  const isAgent = kind === 'agent'
  const options = isAgent
    ? resources.harnesses.filter((profile) => !profile.archived)
    : resources.connections.filter((conn) => !conn.archived)
  const groups = isAgent ? resources.groups.agent : resources.groups.llm
  const setDirect = (patch: Partial<BindingSelectionDraft>) => {
    onChange({ ...draft, ...patch, kind: 'direct' })
  }
  const updateGroup = (groupId: string) => {
    onChange({ kind: 'group', group_id: groupId })
  }
  return (
    <fieldset className="single-agent-row" aria-label="Выбор исполнителя">
      <legend>Выбор исполнителя</legend>
      <div className="group-toggle" role="tablist" aria-label="Тип выбора">
        <button
          type="button"
          role="tab"
          aria-selected={draft.kind === null}
          className={`quiet tab${draft.kind === null ? ' selected' : ''}`}
          onClick={() => onChange({ kind: null })}
        >
          Наследовать
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={draft.kind === 'direct'}
          className={`quiet tab${draft.kind === 'direct' ? ' selected' : ''}`}
          onClick={() => {
            if (draft.kind === 'direct') return
            onChange({ kind: 'direct', model_id: '' })
          }}
        >
          Прямая модель
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={draft.kind === 'group'}
          className={`quiet tab${draft.kind === 'group' ? ' selected' : ''}`}
          onClick={() => {
            if (draft.kind === 'group') return
            onChange({ kind: 'group', group_id: '' })
          }}
        >
          Группа
        </button>
      </div>
      {draft.kind === null ? (
        <p className="hint">
          Будет использован выбор из привязки или узла графа.
        </p>
      ) : null}
      {draft.kind === 'direct' ? (
        <>
          <label htmlFor="single-resource">
            {isAgent ? 'Harness-профиль' : 'LLM-подключение'}
          </label>
          <select
            id="single-resource"
            value={
              isAgent
                ? (draft.harness_profile_id ?? '')
                : (draft.provider_connection_id ?? '')
            }
            onChange={(event) => {
              if (isAgent) {
                setDirect({
                  harness_profile_id: event.target.value,
                  provider_connection_id: undefined,
                })
              } else {
                setDirect({
                  provider_connection_id: event.target.value,
                  harness_profile_id: undefined,
                })
              }
            }}
          >
            <option value="" disabled>
              {options.length === 0
                ? 'Нет доступных ресурсов'
                : 'Выберите ресурс'}
            </option>
            {options.map((option) => (
              <option key={option.id} value={option.id}>
                {option.name}
              </option>
            ))}
          </select>
          <label htmlFor="single-model-id">ID модели</label>
          <input
            id="single-model-id"
            value={draft.model_id ?? ''}
            onChange={(event) => setDirect({ model_id: event.target.value })}
            spellCheck={false}
          />
        </>
      ) : null}
      {draft.kind === 'group' ? (
        <>
          <label htmlFor="single-group">Группа моделей</label>
          <select
            id="single-group"
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
          {draft.group_id ? (
            <span className="muted">
              {groups
                .find((group) => group.id === draft.group_id)
                ?.members.filter((member) => member.enabled)
                .map((member) => member.model_id)
                .join(', ') || 'нет включённых кандидатов'}
            </span>
          ) : null}
        </>
      ) : null}
    </fieldset>
  )
}

function LaunchPreview({
  report,
  resolved,
  singleAgent,
}: {
  report: PreflightReport
  resolved: ResolvedSettings | null
  singleAgent: ApiSchemas['SingleAgentSpec'] | undefined
}) {
  const preview = report.preview
  const effectiveSingle = (preview.single_agent ?? singleAgent) as
    ApiSchemas['SingleAgentSpec'] | undefined
  const candidates = (preview.candidates ?? {}) as Record<
    string,
    Array<Record<string, unknown>>
  >
  return (
    <>
      <p role="status">
        {report.ok
          ? 'Проверка пройдена. Доступность ресурсов повторно проверит worker перед исполнением.'
          : 'Проверка выявила ошибки.'}
      </p>
      {effectiveSingle ? (
        <p className="hint">
          Запускается один агент: <code>{effectiveSingle.role}</code> · узел{' '}
          <code>{effectiveSingle.node_id}</code>
          {effectiveSingle.selection
            ? ` · ${
                effectiveSingle.selection.kind === 'group'
                  ? `группа ${String(effectiveSingle.selection.group_id)}`
                  : `прямая модель ${String(effectiveSingle.selection.model_id)}`
              }`
            : ' · наследуемый прямой выбор; параметры ниже'}
        </p>
      ) : null}
      {effectiveSingle && preview.graph ? (
        <details>
          <summary>Граф одиночного запуска</summary>
          <pre>{JSON.stringify(preview.graph, null, 2)}</pre>
        </details>
      ) : null}
      {(['errors', 'warnings'] as const).map((kind) =>
        report[kind].length ? (
          <ul
            key={kind}
            aria-label={
              kind === 'errors'
                ? 'Ошибки preflight'
                : 'Предупреждения preflight'
            }
          >
            {report[kind].map((issue, index) => (
              <li
                key={`${issue.code}-${index}`}
                className={kind === 'errors' ? 'error' : 'hint'}
              >
                <code>{issue.code}</code>
                {issue.node_id ? ` · ${issue.node_id}` : ''}: {issue.message}
                {issue.details && Object.keys(issue.details).length ? (
                  <pre>{JSON.stringify(issue.details, null, 2)}</pre>
                ) : null}
              </li>
            ))}
          </ul>
        ) : null,
      )}
      {report.execution_hash ? (
        <p className="hash-line">
          execution_hash проверенного запуска:{' '}
          <code>{report.execution_hash}</code>
        </p>
      ) : null}
      <section className="resolved-roles" aria-label="Будут использованы">
        <strong>Будут использованы</strong>
        <ul>
          {Object.entries(resolved?.roles ?? {}).map(([role, info]) => (
            <li key={role}>
              <strong>{role}</strong> · {info.kind} ·{' '}
              {info.selection?.kind === 'group'
                ? `группа ${String(info.selection.group_id)}`
                : (info.model_id ?? 'выбор на уровне узла')}
              {info.harness_profile_id
                ? ` · профиль ${info.harness_profile_id}`
                : ''}
              {info.provider_connection_id
                ? ` · подключение ${info.provider_connection_id}`
                : ''}
            </li>
          ))}
        </ul>
        {Object.entries(candidates).map(([node, rows]) => (
          <div key={node}>
            <h4>Узел {node}</h4>
            <ol>
              {rows.map((candidate, index) => (
                <li key={index}>
                  <strong>{String(candidate.model_id)}</strong> · позиция{' '}
                  {index + 1} · {String(candidate.status)}
                  {candidate.reason ? ` · ${String(candidate.reason)}` : ''}
                  <details>
                    <summary>Профиль, параметры и назначение</summary>
                    <pre>{JSON.stringify(candidate, null, 2)}</pre>
                  </details>
                </li>
              ))}
            </ol>
          </div>
        ))}
        {resolved?.warnings?.map((warning, index) => (
          <p className="hint" key={index}>
            {warning.message}
          </p>
        ))}
        <details>
          <summary>Итоговые настройки и происхождение</summary>
          {singleAgent ? (
            <pre>
              {JSON.stringify(
                {
                  settings: preview.resolved_settings,
                  sources: preview.setting_sources,
                  single_agent: preview.single_agent,
                  candidates,
                },
                null,
                2,
              )}
            </pre>
          ) : (
            <table className="provenance-table">
              <thead>
                <tr>
                  <th>Настройка</th>
                  <th>Источник</th>
                  <th>Значение</th>
                </tr>
              </thead>
              <tbody>
                {resolved?.provenance?.map((item) => (
                  <tr key={item.name}>
                    <td>{item.name}</td>
                    <td>{formatSettingSource(item.source)}</td>
                    <td>
                      <pre>{JSON.stringify(item.value, null, 2)}</pre>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </details>
        <details>
          <summary>Входы, команды, назначения данных и Git</summary>
          <pre>
            {JSON.stringify(
              {
                inputs: preview.inputs,
                resolved_settings: preview.resolved_settings,
                setting_sources: preview.setting_sources,
                commands: preview.commands,
                data_destinations: preview.data_destinations,
                permissions: preview.permissions,
                git_plan: preview.git_plan,
              },
              null,
              2,
            )}
          </pre>
        </details>
      </section>
    </>
  )
}
interface ChatRunsListProps {
  projectId: string
  chatId: string
  onSelectRun: (runId: string) => void
}

export function ChatRunsList({
  projectId,
  chatId,
  onSelectRun,
}: ChatRunsListProps) {
  const runs = useQuery({
    queryKey: ['runs_for_chat', { projectId, chatId }],
    queryFn: () => runsApi.list({ projectId, chatId }),
    enabled: Boolean(projectId && chatId),
    refetchInterval: 5_000,
  })
  if (runs.isLoading) {
    return (
      <p role="status" className="panel-empty">
        Загружаем запуски диалога…
      </p>
    )
  }
  if (runs.error) {
    return (
      <p className="error">
        {runs.error instanceof ApiError
          ? runs.error.body.message
          : 'Не удалось получить запуски'}
      </p>
    )
  }
  if (!runs.data?.length) {
    return <p className="panel-empty">В этом диалоге ещё не было запусков.</p>
  }
  return (
    <ul className="panel-list" aria-label="Запуски диалога">
      {runs.data.map((run) => (
        <li key={run.id} className="profile-item">
          <header>
            <strong>{shortHash(run.id, 12)}</strong>
            <span className="pill">{run.state}</span>
          </header>
          <span className="meta">
            <span>{formatDateTime(run.created_at)}</span>
            <button
              type="button"
              className="quiet"
              onClick={() => onSelectRun(run.id)}
            >
              Открыть
            </button>
          </span>
        </li>
      ))}
    </ul>
  )
}
