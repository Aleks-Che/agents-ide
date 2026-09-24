import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Pause, Play, RotateCcw, Square } from 'lucide-react'
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import { ApiError } from '../../api/client'
import {
  describeRunError,
  runsApi,
  type EventEnvelope,
  type RunCommand,
  type RunObservation,
  type RunRecord,
} from '../../api/runs'
import { useCsrfToken } from '../../app/session'
import { ActivitySpinner } from '../../app/ActivityIndicators'
import { ContextMenu } from '../../app/ContextMenu'
import {
  contextMenuHandlers,
  type ContextMenuTarget,
} from '../../app/context_menu'
import { useRunEventSource } from '../../app/useRunStream'
import {
  allowedCommands,
  pendingAgentRecovery,
  pendingGitChanges,
} from './controls'
import {
  mergeEvents,
  stateDescriptions,
  waitingDescriptions,
} from './observation'
import { stageOutput } from './stage_output'
import { ArtifactDetail } from './ArtifactDetail'
import { RunWorkspace } from './RunWorkspace'
import { ResolutionForm } from './RunsPanel'
import { LoopProgress } from './LoopProgress'
import { RunErrorAlert } from './RunErrorAlert'

type Stage = NonNullable<RunObservation['nodes']>[number]
const stageStates: Record<string, string> = {
  pending: 'Ожидает',
  running: 'Выполняется',
  succeeded: 'Завершён',
  failed: 'Ошибка',
  waiting_input: 'Нужно решение',
  interrupted: 'Прерван',
  retry_wait: 'Повторная попытка',
  skipped: 'Пропущен',
}
const activeRunStates = new Set([
  'queued',
  'running',
  'pause_requested',
  'stop_requested',
  'retry_wait',
  'recovering',
])

function StageStatus({
  node,
  run,
  current,
  readyToContinue,
}: {
  node: Stage
  run?: RunRecord
  current: boolean
  readyToContinue: boolean
}) {
  return (
    <span className="execution-status">
      {current &&
      run &&
      activeRunStates.has(run.state) &&
      !['succeeded', 'failed', 'skipped'].includes(node.status ?? 'pending') ? (
        <ActivitySpinner />
      ) : null}
      {current && run?.state === 'waiting_input'
        ? readyToContinue
          ? 'Готов к продолжению'
          : 'Нужно решение'
        : (stageStates[node.status ?? 'pending'] ?? node.status)}
    </span>
  )
}

function orderStages(observation?: RunObservation | null): Stage[] {
  const nodes = observation?.nodes ?? []
  const ordered: Stage[] = []
  const visit = (id: string) => {
    const node = nodes.find((n) => n.id === id)
    if (!node || ordered.some((n) => n.id === id)) return
    ordered.push(node)
    for (const edge of observation?.edges ?? [])
      if (edge.source === id) visit(edge.target)
  }
  for (const node of nodes.filter((n) => n.type === 'Start')) visit(node.id)
  for (const node of nodes) visit(node.id)
  return ordered
}

export function ChatRunProgress({
  runId,
  onDetails,
  onNewRun,
  onRestarted,
}: {
  runId: string
  onDetails: () => void
  onNewRun: () => void
  onRestarted: (runId: string) => void
}) {
  const csrf = useCsrfToken()
  const client = useQueryClient()
  const [live, setLive] = useState<EventEnvelope[]>([])
  const [resolution, setResolution] = useState(false)
  const [stageMenu, setStageMenu] = useState<{
    target: ContextMenuTarget
    nodeId: string
  } | null>(null)
  const [selectedStage, setSelectedStage] = useState<{
    cursor: string
    id: string
  } | null>(null)
  const snapshot = useQuery({
    queryKey: ['run_snapshot', runId],
    queryFn: () => runsApi.snapshot(runId),
    refetchInterval: 2500,
  })
  const bootstrap = useQuery({
    queryKey: ['chat_run_stream', runId],
    queryFn: async () => {
      const data = await runsApi.snapshot(runId)
      const history = await runsApi.history(runId, {
        before: data.last_sequence + 1,
      })
      return { after: data.last_sequence, events: history.events }
    },
    staleTime: Infinity,
    gcTime: 0,
  })
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ['run_snapshot', runId] })
    void client.invalidateQueries({ queryKey: ['runs_for_chat'] })
    void client.invalidateQueries({ queryKey: ['runs_summary'] })
    void client.invalidateQueries({ queryKey: ['sidebar_activity'] })
  }
  const stream = useRunEventSource({
    runId,
    enabled: Boolean(bootstrap.data),
    after: bootstrap.data?.after ?? 0,
    onEvent: (event) => {
      setLive((previous) => mergeEvents(previous, [event]))
      if (
        [
          'node.entered',
          'node.left',
          'run.state_changed',
          'run.waiting_input',
          'agent.input_requested',
          'agent.input_closed',
          'budget.updated',
          'transition.selected',
        ].includes(event.type)
      )
        refresh()
    },
    onSnapshot: async (data) => {
      client.setQueryData(['run_snapshot', runId], data)
      const page = await runsApi.history(runId, {
        before: data.last_sequence + 1,
      })
      setLive(page.events)
      void client.invalidateQueries({ queryKey: ['chat_stage_history', runId] })
    },
    onFinalState: refresh,
  })
  const command = useMutation({
    mutationFn: (body: RunCommand) => runsApi.submitCommand(runId, body, csrf),
    onSuccess: (_, body) => {
      setResolution(false)
      if (body.command_type === 'restart_stage')
        void client.invalidateQueries({ queryKey: ['chat_run_stream', runId] })
      refresh()
    },
    onError: refresh,
  })
  const restart = useMutation({
    mutationFn: (body: {
      command_id: string
      expected_state_version: number
    }) => runsApi.restart(runId, body, csrf),
    onSuccess: (next) => {
      refresh()
      onRestarted(next.id)
    },
    onError: refresh,
  })
  const restartUncertain =
    restart.isError &&
    (!(restart.error instanceof ApiError) || restart.error.status >= 500)
  const uncertain =
    command.isError &&
    (!(command.error instanceof ApiError) || command.error.status >= 500)
  const run = snapshot.data?.run
  const send = (
    type: RunCommand['command_type'],
    payload: Record<string, unknown> = {},
  ) => {
    if (!run || command.isPending || uncertain) return
    command.mutate({
      command_id: crypto.randomUUID(),
      command_type: type,
      expected_state_version: run.state_version,
      payload,
    })
  }
  const observation = snapshot.data?.observation
  const current = observation?.current_node_id
  const cursor = `${current}:${observation?.current_execution_id}`
  const nodes = orderStages(observation)
  const selectedId =
    selectedStage?.cursor === cursor ? selectedStage.id : current
  const opened = nodes.find((node) => node.id === selectedId) ?? nodes[0]
  const events = mergeEvents(bootstrap.data?.events ?? [], live)
  const actions = run ? allowedCommands(run) : []
  const waiting = run?.waiting_reason
  const readyToContinue = pendingAgentRecovery(run) || pendingGitChanges(run)
  const menuNode = nodes.find((node) => node.id === stageMenu?.nodeId)
  return (
    <section className="chat-run-progress" aria-label="Выполнение шаблона">
      <header className="chat-run-header">
        <div>
          <h3>Выполнение шаблона</h3>
          <span role="status" className="execution-status">
            {run && activeRunStates.has(run.state) ? <ActivitySpinner /> : null}
            {readyToContinue
              ? 'Готов к продолжению'
              : run
                ? (stateDescriptions[run.state] ?? run.state)
                : 'Загружаем…'}
          </span>
        </div>
        <div className="actions">
          <button
            type="button"
            className="quiet run-icon"
            aria-label={
              actions.includes('resume')
                ? 'Продолжить выполнение'
                : 'Начать новый запуск'
            }
            title={
              actions.includes('resume')
                ? run?.state === 'stopped'
                  ? 'START — начать текущий этап заново'
                  : 'START — продолжить с сохранённым контекстом'
                : 'START — новый запуск'
            }
            disabled={
              !csrf ||
              command.isPending ||
              restart.isPending ||
              restartUncertain ||
              uncertain ||
              (run &&
                !actions.includes('resume') &&
                !['completed', 'cancelled', 'failed'].includes(run.state))
            }
            onClick={() =>
              actions.includes('resume') ? send('resume') : onNewRun()
            }
          >
            <Play aria-hidden="true" size={14} />
          </button>
          <button
            type="button"
            className="quiet run-icon"
            aria-label="Приостановить выполнение"
            title="PAUSE — сохранить сессию агента; остальные операции завершатся перед паузой"
            disabled={
              !csrf ||
              command.isPending ||
              restart.isPending ||
              restartUncertain ||
              uncertain ||
              run?.state === 'pause_requested' ||
              !actions.includes('pause')
            }
            onClick={() => send('pause')}
          >
            <Pause aria-hidden="true" size={14} />
          </button>
          <button
            type="button"
            className="quiet danger run-icon"
            aria-label="Остановить выполнение"
            title="STOP — остановить; START начнёт текущий этап заново"
            disabled={
              !csrf ||
              command.isPending ||
              restart.isPending ||
              restartUncertain ||
              uncertain ||
              !actions.includes('stop')
            }
            onClick={() => send('stop')}
          >
            <Square aria-hidden="true" size={14} />
          </button>
          <button
            type="button"
            className="quiet run-icon"
            aria-label="Перезапустить с первого этапа"
            title="RESTART — заново с первого этапа"
            disabled={
              !run ||
              !csrf ||
              command.isPending ||
              uncertain ||
              restart.isPending ||
              restartUncertain
            }
            onClick={() =>
              run &&
              restart.mutate({
                command_id: crypto.randomUUID(),
                expected_state_version: run.state_version,
              })
            }
          >
            <RotateCcw aria-hidden="true" size={14} />
          </button>
          <button type="button" className="quiet" onClick={onDetails}>
            Подробности
          </button>
        </div>
      </header>
      <RunWorkspace run={run} />
      {snapshot.error || bootstrap.error ? (
        <p role="alert" className="error">
          {describeRunError(snapshot.error ?? bootstrap.error)}{' '}
          <button
            onClick={() => {
              void snapshot.refetch()
              void bootstrap.refetch()
            }}
          >
            Повторить
          </button>
        </p>
      ) : null}
      {['connecting', 'reset_required', 'auth_expired'].includes(
        stream.state,
      ) ? (
        <p role="status">
          {stream.state === 'auth_expired'
            ? 'Сессия истекла. Подключитесь повторно.'
            : 'Восстанавливаем поток сообщений…'}
        </p>
      ) : null}
      <LoopProgress
        observation={observation}
        disabled={
          !csrf ||
          command.isPending ||
          restart.isPending ||
          restartUncertain ||
          uncertain
        }
        onAdjust={(key, delta) => send('adjust_loop', { loop_key: key, delta })}
      />
      {waiting ? (
        <div className="stage-waiting" role="status">
          <strong>
            Этап {current}:{' '}
            {readyToContinue ? 'готов к продолжению' : 'требуется действие'}
          </strong>
          <p>
            {readyToContinue
              ? 'Решение сохранено. Нажмите «Продолжить выполнение». Сервер повторно проверит условия текущего этапа.'
              : waiting.details?.reason === 'process_not_responding'
                ? waitingDescriptions.process_not_responding
                : waiting.details?.reason === 'event_stream_lost'
                  ? 'Потеряна связь с агентом. Последние действия сохранены в журнале; выполнение остановлено.'
                  : waiting.details?.reason === 'runtime_expression_invalid'
                    ? 'Не удалось подставить результат предыдущего этапа в промпт. Проверьте ссылку на поле отчёта.'
                    : waiting.details?.reason ===
                        'provider_tool_protocol_invalid'
                      ? 'Модель повторяет служебные маркеры вместо вызовов инструментов. Генерация остановлена. Проверьте режим доступа harness и перезапустите задание.'
                      : (waitingDescriptions[waiting.code] ?? waiting.code)}
          </p>
          {typeof waiting.details?.message === 'string' ? (
            <p>{waiting.details.message}</p>
          ) : null}
          <details>
            <summary>Причина остановки</summary>
            <pre>{JSON.stringify(waiting.details, null, 2)}</pre>
          </details>
          {readyToContinue ? (
            <button
              type="button"
              disabled={
                !csrf ||
                command.isPending ||
                restart.isPending ||
                restartUncertain ||
                uncertain ||
                !actions.includes('resume')
              }
              onClick={() => send('resume')}
            >
              Продолжить выполнение
            </button>
          ) : null}
          {actions.includes('resolve') &&
          waiting.code !== 'configuration_invalid' ? (
            <button onClick={() => setResolution(!resolution)}>
              {readyToContinue ? 'Изменить решение' : 'Предоставить решение'}
            </button>
          ) : null}
        </div>
      ) : null}
      {command.error ? (
        <p className="error" role="alert">
          {describeRunError(command.error)}
        </p>
      ) : null}
      {restart.isPending ? (
        <p role="status">Готовим перезапуск с первого этапа…</p>
      ) : null}
      {restart.error ? <RunErrorAlert error={restart.error} /> : null}
      {restartUncertain ? (
        <button
          onClick={() => restart.variables && restart.mutate(restart.variables)}
        >
          Проверить перезапуск
        </button>
      ) : null}
      {uncertain ? (
        <button
          onClick={() => command.variables && command.mutate(command.variables)}
        >
          Проверить отправленную команду
        </button>
      ) : null}
      {resolution && run ? (
        <ResolutionForm
          run={run}
          busy={command.isPending || uncertain || !csrf}
          onSubmit={(payload) => send('resolve', payload)}
          onClose={() => setResolution(false)}
        />
      ) : null}
      <div className="chat-stages-layout">
        <nav className="stage-navigation" aria-label="Этапы выполнения">
          {nodes.map((node, i) => (
            <button
              key={node.id}
              type="button"
              className={`quiet${opened?.id === node.id ? ' selected' : ''}`}
              aria-current={opened?.id === node.id ? 'step' : undefined}
              {...contextMenuHandlers((target) =>
                setStageMenu({ target, nodeId: node.id }),
              )}
              title="Правая кнопка мыши — действия с этапом"
              onClick={() => setSelectedStage({ cursor, id: node.id })}
            >
              <span>
                {i + 1}. {node.label}
              </span>
              <small>
                <StageStatus
                  node={node}
                  run={run}
                  current={node.id === current}
                  readyToContinue={readyToContinue}
                />
              </small>
            </button>
          ))}
        </nav>
        <div className="stage-cards">
          {opened ? (
            <article
              id={`stage-${runId}-${opened.id}`}
              className={`stage-card${opened.id === current ? ' current' : ''}`}
              key={opened.id}
            >
              <header
                className="stage-heading"
                tabIndex={0}
                {...contextMenuHandlers((target) =>
                  setStageMenu({ target, nodeId: opened.id }),
                )}
                title="Правая кнопка мыши — действия с этапом"
              >
                <strong>
                  {nodes.indexOf(opened) + 1}. {opened.label}
                </strong>
                <StageStatus
                  node={opened}
                  run={run}
                  current={opened.id === current}
                  readyToContinue={readyToContinue}
                />
              </header>
              {run ? (
                <StageContent
                  key={`${opened.id}:${opened.execution_id}`}
                  runId={runId}
                  run={run}
                  node={opened}
                  current={opened.id === current}
                  events={events}
                />
              ) : null}
            </article>
          ) : null}
        </div>
      </div>
      {stageMenu && menuNode ? (
        <ContextMenu
          target={stageMenu.target}
          label={`Действия с этапом ${menuNode.label}`}
          onClose={() => setStageMenu(null)}
          items={[
            {
              label: 'Продолжить',
              title:
                menuNode.id !== current
                  ? 'Продолжить можно только текущий этап.'
                  : run?.state === 'stopped'
                    ? 'Продолжить выполнение с новой попытки текущего этапа.'
                    : 'Продолжить текущий этап с сохранённого места.',
              disabled:
                !csrf ||
                menuNode.id !== current ||
                !actions.includes('resume') ||
                command.isPending ||
                restart.isPending ||
                uncertain ||
                restartUncertain,
              onSelect: () => send('resume'),
            },
            {
              label: 'Перезапустить этап',
              title:
                menuNode.restart_blocked_reason ??
                'Начать этот этап заново и продолжить шаблон. Изменения файлов сохранятся.',
              disabled:
                !csrf ||
                !menuNode.execution_id ||
                !!menuNode.restart_blocked_reason ||
                command.isPending ||
                restart.isPending ||
                uncertain ||
                restartUncertain,
              onSelect: () =>
                send('restart_stage', {
                  node_id: menuNode.id,
                  execution_id: menuNode.execution_id,
                }),
            },
          ]}
        />
      ) : null}
    </section>
  )
}

function StageContent({
  runId,
  run,
  node,
  current,
  events,
}: {
  runId: string
  run: RunRecord
  node: Stage
  current: boolean
  events: EventEnvelope[]
}) {
  const history = useInfiniteQuery({
    queryKey: ['chat_stage_history', runId, node.id],
    initialPageParam: undefined as number | undefined,
    queryFn: ({ pageParam }) =>
      runsApi.history(runId, { nodeId: node.id, before: pageParam }),
    getNextPageParam: (page) =>
      page.has_more && page.next_before !== null ? page.next_before : undefined,
    refetchInterval: current ? 5000 : false,
    gcTime: 0,
  })
  const [artifact, setArtifact] = useState(false)
  const all = [
    ...new Map(
      [
        ...(history.data?.pages.flatMap((page) => page.events) ?? []),
        ...events.filter((event) => event.node_id === node.id),
      ].map((event) => [event.sequence, event]),
    ).values(),
  ].sort((a, b) => a.sequence - b.sequence)
  const questions = all
    .filter(
      (event) =>
        event.type === 'agent.input_requested' &&
        event.step_attempt_id === node.attempt_id &&
        Array.isArray(event.payload.questions),
    )
    .filter(
      (question) =>
        !all.some(
          (event) =>
            event.type === 'agent.input_closed' &&
            event.step_attempt_id === question.step_attempt_id &&
            event.payload.question_id === question.payload.question_id,
        ),
    )
  const storedQuestion = node.input_request
  const question =
    questions.at(-1) ??
    (storedQuestion &&
    !all.some(
      (event) =>
        event.type === 'agent.input_closed' &&
        event.step_attempt_id === node.attempt_id &&
        event.payload.question_id === storedQuestion.question_id,
    )
      ? { payload: storedQuestion }
      : undefined)
  const output = stageOutput(all)
  const log = useRef<HTMLDivElement>(null)
  const logContent = useRef<HTMLDivElement>(null)
  const [following, setFollowing] = useState(true)
  // Scroll after every rendered update: history and tool updates can change
  // the output without advancing the last event sequence.
  useLayoutEffect(() => {
    if (following && log.current)
      log.current.scrollTop = log.current.scrollHeight
  })
  useEffect(() => {
    const element = log.current
    const content = logContent.current
    if (!following || !element || !content) return
    const observer = new ResizeObserver(() => {
      element.scrollTop = element.scrollHeight
    })
    observer.observe(content)
    observer.observe(element)
    return () => observer.disconnect()
  }, [following])
  return (
    <div className="stage-content">
      <p className="muted">
        {node.type} · {node.model_id ?? 'Без модели'} · посещение{' '}
        {node.visit_index ?? 0}
        {node.type === 'AgentTask' && node.context_tokens != null ? (
          <>
            {' · '}
            <span
              title={`Используемый контекст: ${node.context_tokens.toLocaleString('ru-RU')} токенов`}
            >
              {node.context_tokens >= 1000
                ? `${Number((node.context_tokens / 1000).toFixed(1))}k`
                : node.context_tokens}
            </span>
          </>
        ) : null}
      </p>
      {history.hasNextPage ? (
        <button
          type="button"
          disabled={history.isFetchingNextPage}
          onClick={() => {
            setFollowing(false)
            void history.fetchNextPage()
          }}
        >
          Показать предыдущие сообщения этапа
        </button>
      ) : null}
      {history.error ? (
        <p role="alert">{describeRunError(history.error)}</p>
      ) : null}
      <div
        ref={log}
        className="stage-output"
        role="log"
        aria-label={`Сообщения этапа ${node.label}`}
        onScroll={() => {
          const el = log.current
          if (el)
            setFollowing(el.scrollHeight - el.scrollTop - el.clientHeight < 40)
        }}
      >
        <div ref={logContent}>
          {output.map((entry) => (
            <div
              className={`stage-message ${entry.role === 'user' ? 'role-user' : entry.role === 'system' ? 'role-system' : 'role-assistant'}`}
              key={entry.key}
            >
              <small>
                <time
                  dateTime={new Date(entry.occurredAt * 1000).toISOString()}
                >
                  {new Date(entry.occurredAt * 1000).toLocaleDateString(
                    'ru-RU',
                    {
                      day: '2-digit',
                      month: '2-digit',
                      year: 'numeric',
                    },
                  )}{' '}
                  {new Date(entry.occurredAt * 1000).toLocaleTimeString(
                    'ru-RU',
                    {
                      hour: '2-digit',
                      minute: '2-digit',
                      second: '2-digit',
                      hour12: false,
                    },
                  )}
                </time>{' '}
                {entry.role === 'user'
                  ? 'Вы'
                  : entry.role === 'system'
                    ? 'Этап'
                    : 'Агент'}
              </small>
              <pre>
                {entry.tool && entry.tool.count > 1
                  ? `(${entry.tool.count}) `
                  : null}
                {entry.text}
              </pre>
            </div>
          ))}
          {!output.length ? (
            <p className="muted">
              {history.isLoading
                ? 'Загружаем сообщения…'
                : node.status === 'pending'
                  ? 'Этап ещё не начат.'
                  : 'Сообщений на этом этапе пока нет.'}
            </p>
          ) : null}
        </div>
      </div>
      {!following ? (
        <button
          type="button"
          className="quiet stage-follow"
          onClick={() => setFollowing(true)}
        >
          К последним сообщениям ↓
        </button>
      ) : null}
      {node.result_ref ? (
        <>
          <button className="quiet" onClick={() => setArtifact(!artifact)}>
            {artifact ? 'Скрыть итоговый отчёт' : 'Итоговый отчёт'}
          </button>
          {artifact ? (
            <ArtifactDetail runId={runId} artifactId={node.result_ref} />
          ) : null}
        </>
      ) : null}
      {current &&
      run.state === 'running' &&
      node.type === 'AgentTask' &&
      node.attempt_id ? (
        <AgentReply
          key={`${node.attempt_id}:${question?.payload.question_id ?? ''}`}
          runId={runId}
          run={run}
          node={node}
          question={question}
        />
      ) : null}
    </div>
  )
}

function AgentReply({
  runId,
  run,
  node,
  question,
}: {
  runId: string
  run: RunRecord
  node: Stage
  question?: { payload: Record<string, unknown> }
}) {
  const client = useQueryClient()
  const csrf = useCsrfToken()
  const [text, setText] = useState('')
  const [answers, setAnswers] = useState<Record<number, string>>({})
  const questions = (
    Array.isArray(question?.payload.questions) ? question.payload.questions : []
  ) as Array<{ question: string; options: string[] }>
  const permission =
    question?.payload.kind === 'permission' ? question.payload : undefined
  const mutation = useMutation({
    mutationFn: (body: RunCommand) => runsApi.submitCommand(runId, body, csrf),
    onSuccess: () => {
      setText('')
      setAnswers({})
      void client.invalidateQueries({ queryKey: ['chat_stage_history', runId] })
      void client.invalidateQueries({ queryKey: ['run_snapshot', runId] })
    },
    onError: () => {
      void client.invalidateQueries({ queryKey: ['run_snapshot', runId] })
    },
  })
  const uncertain =
    mutation.isError &&
    (!(mutation.error instanceof ApiError) || mutation.error.status >= 500)
  const ready = questions.length
    ? questions.every((_, i) => answers[i]?.trim())
    : text.trim()
  return (
    <form
      className="agent-reply"
      onSubmit={(event) => {
        event.preventDefault()
        if (!ready || !csrf || mutation.isPending || uncertain) return
        mutation.mutate({
          command_id: crypto.randomUUID(),
          command_type: 'message',
          expected_state_version: run.state_version,
          payload: {
            attempt_id: node.attempt_id,
            text: questions.length
              ? questions
                  .map((q, i) => `${q.question}\n${answers[i]}`)
                  .join('\n\n')
              : text.trim(),
            ...(question
              ? {
                  question_id: question.payload.question_id,
                  answers: questions.map((_, i) => [answers[i]]),
                }
              : {}),
          },
        })
      }}
    >
      {permission ? (
        <>
          <strong>Агент запрашивает разрешение</strong>
          <p>{String(permission.permission ?? '')}</p>
          <pre>
            {JSON.stringify(
              { patterns: permission.patterns, details: permission.metadata },
              null,
              2,
            )}
          </pre>
          {(['once', 'reject'] as const).map((reply) => (
            <button
              key={reply}
              type="button"
              disabled={
                !csrf || mutation.isPending || mutation.isSuccess || uncertain
              }
              onClick={() =>
                mutation.mutate({
                  command_id: crypto.randomUUID(),
                  command_type: 'message',
                  expected_state_version: run.state_version,
                  payload: {
                    attempt_id: node.attempt_id,
                    permission_id: permission.permission_id,
                    permission_reply: reply,
                    text:
                      reply === 'once'
                        ? 'Разрешить действие один раз'
                        : 'Отклонить действие',
                  },
                })
              }
            >
              {reply === 'once' ? 'Разрешить один раз' : 'Отклонить'}
            </button>
          ))}
        </>
      ) : questions.length ? (
        <>
          <strong>Агент ожидает ответа</strong>
          {questions.map((q, i) => (
            <label key={i}>
              {q.question}
              <textarea
                value={answers[i] ?? ''}
                onChange={(event) =>
                  setAnswers({ ...answers, [i]: event.target.value })
                }
                required
                rows={2}
              />
              {q.options?.map((option) => (
                <button
                  type="button"
                  className="quiet"
                  key={option}
                  onClick={() => setAnswers({ ...answers, [i]: option })}
                >
                  {option}
                </button>
              ))}
            </label>
          ))}
        </>
      ) : (
        <label>
          Сообщение текущему агенту
          <textarea
            value={text}
            onChange={(event) => setText(event.target.value)}
            rows={2}
            required
            placeholder="Уточните задачу или ответьте агенту…"
          />
        </label>
      )}
      {mutation.error ? (
        <p className="error" role="alert">
          {describeRunError(mutation.error)}
        </p>
      ) : null}
      {mutation.isSuccess ? (
        <p role="status">Сообщение сохранено для передачи текущему агенту.</p>
      ) : null}
      {uncertain ? (
        <button
          type="button"
          onClick={() =>
            mutation.variables && mutation.mutate(mutation.variables)
          }
        >
          Проверить отправку сообщения
        </button>
      ) : null}
      {!permission ? (
        <button
          type="submit"
          disabled={!ready || !csrf || mutation.isPending || uncertain}
        >
          {mutation.isPending ? 'Отправляем…' : 'Отправить агенту'}
        </button>
      ) : null}
    </form>
  )
}
