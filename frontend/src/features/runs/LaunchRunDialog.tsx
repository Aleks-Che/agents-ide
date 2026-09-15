import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError } from '../../api/client'
import {
  bindingsApi,
  templatesApi,
  type PipelineBinding,
  type PreflightReport,
  type ResolvedSettings,
} from '../../api/bindings'
import { runsApi, type RunRecord } from '../../api/runs'
import type { Chat, Project } from '../../api/projects'
import { Modal } from '../../app/Modal'
import { formatDateTime, shortHash } from '../../app/format'
import { useCsrfToken } from '../../app/session'
import { formatSettingSource } from '../bindings/utils'
import {
  launchParameters,
  launchError,
  loadPendingStart,
  pendingStartKey,
  uncertainStart,
  type StartRequest,
} from './launch'

interface LaunchRunDialogProps {
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
  const [bindingId, setBindingId] = useState('')
  const [mode, setMode] = useState<'real' | 'simulated'>('real')
  const [useDraft, setUseDraft] = useState(false)
  const [inputsText, setInputsText] = useState('{}')
  const [limitsText, setLimitsText] = useState('{}')
  const [commandsText, setCommandsText] = useState('null')
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
  // Every user-editable launch input invalidates both the preview and import consent.
  const formKey = JSON.stringify([
    bindingId,
    selectedBinding?.version,
    mode,
    inputsText,
    limitsText,
    commandsText,
    useDraft,
    draft,
  ])
  const check = useMutation({
    mutationFn: async () => {
      if (!selectedBinding) throw new Error('Выберите доступную привязку.')
      const parameters = launchParameters(
        mode,
        inputsText,
        limitsText,
        commandsText,
      )
      const message = useDraft && draft.trim() ? draft.trim() : undefined
      if (message && message.length > 65536)
        throw new Error('Черновик превышает 65536 символов.')
      const [report, resolved] = await Promise.all([
        bindingsApi.preflight(bindingId, csrf, parameters),
        bindingsApi
          .resolvedWithOverrides(bindingId, parameters.overrides ?? {}, csrf)
          .then((data) => ({ data, error: null }))
          .catch((error: unknown) => ({
            data: null,
            error: launchError(error),
          })),
      ])
      return { key: formKey, report, resolved, parameters, message, bindingId }
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
  const ready = Boolean(
    checked?.report.ok &&
    checked.report.execution_hash &&
    (!requiresTrust || trustedKey === formKey),
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
            </p>
            <details>
              <summary>Сохранённый запрос</summary>
              <pre>{JSON.stringify(pending, null, 2)}</pre>
            </details>
          </section>
        ) : (
          <>
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
              <legend>Режим</legend>
              <label>
                <input
                  type="radio"
                  name="launch-mode"
                  checked={mode === 'real'}
                  onChange={() => setMode('real')}
                />
                Реальный
              </label>
              <label>
                <input
                  type="radio"
                  name="launch-mode"
                  checked={mode === 'simulated'}
                  onChange={() => setMode('simulated')}
                />
                Имитация (fake)
              </label>
            </fieldset>
            {version.isLoading ? (
              <p role="status">Загружаем входы версии…</p>
            ) : null}
            {version.error ? (
              <p role="alert" className="error">
                {launchError(version.error)}{' '}
                <button type="button" onClick={() => void version.refetch()}>
                  Повторить загрузку версии
                </button>
              </p>
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
                    !csrf
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
                (!ready || Boolean(bindingsError) || !selectedBinding))
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

function LaunchPreview({
  report,
  resolved,
}: {
  report: PreflightReport
  resolved: ResolvedSettings | null
}) {
  const preview = report.preview
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
