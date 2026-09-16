import { lazy, Suspense, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError } from '../../api/client'
import {
  runsApi,
  describeRunError,
  type RunCommand,
  type RunRecord,
  type ArtifactView,
} from '../../api/runs'
import { useCsrfToken } from '../../app/session'
import { formatDateTime, shortHash } from '../../app/format'
import { Modal } from '../../app/Modal'
import { allowedCommands, resolutionPayload } from './controls'
import { GroupSummarySection } from './GroupSummarySection'
import { RunTimeline } from './RunTimeline'
import { ArtifactDetail } from './ArtifactDetail'
import { duration, stateDescriptions, waitingDescriptions } from './observation'
const RunGraph = lazy(() => import('./RunGraph'))

export function RunScreen({
  runId,
  onClose,
}: {
  runId: string
  onClose: () => void
}) {
  const csrf = useCsrfToken()
  const client = useQueryClient()
  const [nodeId, setNodeId] = useState('')
  const [artifactOffset, setArtifactOffset] = useState(0)
  const [commandOffset, setCommandOffset] = useState(0)
  const [selectedArtifact, setSelectedArtifact] = useState<string | null>(null)
  const run = useQuery({
    queryKey: ['run', runId],
    queryFn: () => runsApi.get(runId),
    refetchInterval: 4000,
  })
  const plan = useQuery({
    queryKey: ['run_plan', runId],
    queryFn: () => runsApi.plan(runId),
    refetchInterval: 6000,
  })
  const artifacts = useQuery({
    queryKey: ['run_artifacts', runId, artifactOffset],
    queryFn: () => runsApi.artifacts(runId, artifactOffset),
    gcTime: 0,
    refetchInterval: 6000,
  })
  const diagnostics = useQuery({
    queryKey: ['run_diagnostics', runId],
    queryFn: () => runsApi.diagnostics(runId),
    refetchInterval: 4000,
  })
  const journal = useQuery({
    queryKey: ['run_commands', runId, commandOffset],
    queryFn: () => runsApi.commandJournal(runId, commandOffset),
    gcTime: 0,
    refetchInterval: 5000,
  })
  const snapshot = useQuery({
    queryKey: ['run_snapshot', runId],
    queryFn: () => runsApi.snapshot(runId),
    refetchInterval: 6000,
  })
  const [resolutionOpen, setResolutionOpen] = useState(false)
  const refresh = () => {
    for (const queryKey of [
      ['run', runId],
      ['run_plan', runId],
      ['run_artifacts', runId],
      ['run_diagnostics', runId],
      ['run_commands', runId],
      ['run_snapshot', runId],
      ['runs_summary'],
    ]) {
      void client.invalidateQueries({ queryKey })
    }
  }
  const command = useMutation({
    mutationFn: (payload: RunCommand) =>
      runsApi.submitCommand(runId, payload, csrf),
    onSuccess: () => {
      setResolutionOpen(false)
      refresh()
    },
    onError: refresh,
  })
  const uncertain =
    command.isError &&
    (!(command.error instanceof ApiError) || command.error.status >= 500)
  const send = (
    type: RunCommand['command_type'],
    payload: Record<string, unknown> = {},
  ) => {
    if (!data || command.isPending || uncertain) return
    command.mutate({
      command_id: crypto.randomUUID(),
      command_type: type,
      expected_state_version: data.state_version,
      payload,
    })
  }
  const data =
    snapshot.data &&
    (!run.data || snapshot.data.run.state_version > run.data.state_version)
      ? snapshot.data.run
      : run.data
  const observation = snapshot.data?.observation
  const observedNode = observation?.nodes?.find(
    (node) => node.id === (nodeId || observation.current_node_id),
  )
  const waiting =
    data?.waiting_reason ??
    (data?.runtime?.waiting_reason as RunRecord['waiting_reason'])
  const actions = data
    ? allowedCommands(data, diagnostics.data?.resume_target)
    : []
  return (
    <Modal onClose={onClose} busy={command.isPending} labelledBy="run-title">
      <div className="dialog wide run-screen">
        <header>
          <h3 id="run-title">
            Run · {shortHash(runId, 12)} · {data?.state ?? '…'}
          </h3>
          <button
            type="button"
            className="quiet"
            onClick={onClose}
            aria-label="Закрыть экран Run"
          >
            ×
          </button>
        </header>
        {[run, plan, artifacts, diagnostics, journal, snapshot].map(
          (query, index) =>
            query.error ? (
              <p key={index} role="alert" className="error">
                {
                  [
                    'Run',
                    'План',
                    'Артефакты',
                    'Диагностика',
                    'Журнал команд',
                    'Группа и кандидаты',
                  ][index]
                }
                : {describeRunError(query.error)}
                <button type="button" onClick={() => void query.refetch()}>
                  Повторить загрузку
                </button>
              </p>
            ) : null,
        )}
        {run.isLoading ? <p role="status">Загружаем Run…</p> : null}
        {data ? (
          <>
            <section className="run-meta" aria-label="Сводка Run">
              {snapshot.data?.planning_provenance?.degraded ? (
                <p role="status" className="council-degraded-banner">
                  План Council принят в уменьшенном составе: участников{' '}
                  {snapshot.data.planning_provenance.n_participants_actual}/
                  {snapshot.data.planning_provenance.n_participants_requested}.
                </p>
              ) : null}
              <dl>
                <div>
                  <dt>Состояние</dt>
                  <dd>
                    {data.state} · v{data.state_version}
                    <br />
                    {stateDescriptions[data.state]}
                  </dd>
                </div>
                <div>
                  <dt>Режим</dt>
                  <dd>
                    {data.simulated ? 'Симуляция' : 'Реальное выполнение'}
                  </dd>
                </div>
                <div>
                  <dt>Диалог</dt>
                  <dd>{data.chat_id ?? 'Не указан'}</dd>
                </div>
                <div>
                  <dt>Текущий узел / цикл</dt>
                  <dd>
                    {String(
                      observation?.current_node_id ??
                        data.runtime?.current_node_id ??
                        diagnostics.data?.resume_target.node_id ??
                        '—',
                    )}{' '}
                    / {String(data.runtime?.cycle_id ?? '—')}
                  </dd>
                </div>
                <div>
                  <dt>Бюджет</dt>
                  <dd>
                    {String(data.runtime?.external_calls ?? 0)} вызовов · токены{' '}
                    {data.runtime?.budget_quality === 'unknown' ||
                    !data.runtime?.budget_quality
                      ? 'неизвестно'
                      : String(data.runtime?.tokens_used ?? '—')}{' '}
                    · качество:{' '}
                    {String(data.runtime?.budget_quality ?? 'unknown')}
                  </dd>
                </div>
                <div>
                  <dt>Расчётная стоимость</dt>
                  <dd>
                    {data.runtime?.budget_quality === 'unknown' ||
                    !data.runtime?.budget_quality
                      ? 'неизвестно'
                      : String(data.runtime?.cost_estimated ?? 'неизвестно')}
                  </dd>
                </div>
                <div>
                  <dt>Попытка</dt>
                  <dd>
                    {diagnostics.data?.attempt
                      ? `${diagnostics.data.attempt.id} · ${diagnostics.data.attempt.status}`
                      : 'нет активной'}
                  </dd>
                </div>
              </dl>
            </section>
            {waiting ? (
              <section aria-label="Причина ожидания">
                <h4>{waiting.code}</h4>
                <p>
                  {waitingDescriptions[waiting.code] ??
                    'Предоставьте недостающие данные, указанные в причине ожидания.'}
                </p>
                <pre>{JSON.stringify(waiting.details, null, 2)}</pre>
                <p>Доступные действия: {waiting.allowed_actions.join(', ')}</p>
              </section>
            ) : null}
            <section className="run-controls" aria-label="Управление Run">
              {(['pause', 'stop', 'resume', 'cancel', 'resolve'] as const).map(
                (type) => (
                  <button
                    type="button"
                    key={type}
                    className="quiet"
                    disabled={
                      !csrf ||
                      command.isPending ||
                      uncertain ||
                      !actions.includes(type)
                    }
                    onClick={() =>
                      type === 'resolve' ? setResolutionOpen(true) : send(type)
                    }
                  >
                    {
                      {
                        pause: 'Пауза',
                        stop: 'Остановить',
                        resume: 'Продолжить',
                        cancel: 'Отменить',
                        resolve: 'Решить',
                      }[type]
                    }
                  </button>
                ),
              )}
              {command.data ? (
                <p role="status">
                  {command.data.status} · {command.data.command_id}
                </p>
              ) : null}
              {command.error ? (
                <p role="alert" className="error">
                  {describeRunError(command.error)}
                </p>
              ) : null}
              {uncertain ? (
                <div>
                  <p>
                    Ответ на команду не получен. Проверьте журнал или повторите
                    тот же запрос с прежним command_id.
                  </p>
                  <button
                    type="button"
                    onClick={() =>
                      command.variables && command.mutate(command.variables)
                    }
                  >
                    Проверить отправленную команду
                  </button>
                </div>
              ) : null}
            </section>
            {resolutionOpen ? (
              <ResolutionForm
                key={waiting?.code ?? 'resolution'}
                run={data}
                onSubmit={(payload) => send('resolve', payload)}
                onClose={() => setResolutionOpen(false)}
              />
            ) : null}
          </>
        ) : null}
        {observation ? (
          <section aria-label="Позиция выполнения">
            <h4>Граф и текущий шаг</h4>
            <Suspense fallback={<p>Загружаем граф…</p>}>
              <RunGraph
                observation={observation}
                selected={nodeId}
                onSelect={setNodeId}
              />
            </Suspense>
            <p>
              Последняя выбранная связь:{' '}
              {observation.last_transition
                ? `${String(observation.last_transition.source_node_id)} → ${String(observation.last_transition.target)} · ${String(observation.last_transition.reason)}`
                : '—'}
            </p>
            <label>
              Подробности узла
              <select
                value={nodeId}
                onChange={(event) => setNodeId(event.target.value)}
              >
                <option value="">Текущий узел</option>
                {observation.nodes?.map((node) => (
                  <option key={node.id}>{node.id}</option>
                ))}
              </select>
            </label>
            {observedNode ? (
              <div className="observed-detail">
                <strong>
                  {observedNode.label} · {observedNode.status}
                </strong>
                <p>
                  Посещение {observedNode.visit_index} · цикл{' '}
                  {observedNode.cycle_id ?? '—'} · попыток{' '}
                  {observedNode.attempt_count} · время{' '}
                  {duration(observedNode.started_at, observedNode.finished_at)}
                </p>
                <p>
                  Модель: {observedNode.model_id ?? '—'} · агент / подключение:{' '}
                  {observedNode.resource_id ?? '—'}
                </p>
                <p>
                  Результат проверки:{' '}
                  {observedNode.decision ?? 'не предоставлен'}
                </p>
                {observedNode.result_ref ? (
                  <button
                    onClick={() =>
                      setSelectedArtifact(observedNode.result_ref!)
                    }
                  >
                    Открыть результат шага
                  </button>
                ) : null}
              </div>
            ) : null}
          </section>
        ) : null}
        <RunTimeline
          runId={runId}
          nodeId={nodeId}
          onNodeChange={setNodeId}
          nodes={observation?.nodes?.map((node) => node.id) ?? []}
          onRefresh={refresh}
          onArtifact={setSelectedArtifact}
        />
        {selectedArtifact ? (
          <section aria-label="Выбранный артефакт">
            <button onClick={() => setSelectedArtifact(null)}>
              Закрыть артефакт
            </button>
            <ArtifactDetail
              key={selectedArtifact}
              runId={runId}
              artifactId={selectedArtifact}
            />
          </section>
        ) : null}
        <section aria-label="Состояние процессов">
          <h4>Процессы</h4>
          {diagnostics.isLoading ? (
            <p role="status">Загружаем диагностику…</p>
          ) : diagnostics.data ? (
            diagnostics.data.processes.length ? (
              <ul>
                {diagnostics.data.processes.map((process) => (
                  <li key={process.id}>
                    PID {process.pid ?? '—'} · {process.state} · health:{' '}
                    {process.health ?? 'unknown'} · последняя проверка:{' '}
                    {process.last_health_ok === null
                      ? 'неизвестно'
                      : process.last_health_ok
                        ? 'OK'
                        : 'сбой'}
                  </li>
                ))}
              </ul>
            ) : (
              <p>Активных процессов нет.</p>
            )
          ) : null}
        </section>
        {snapshot.data?.selection ? (
          <GroupSummarySection
            selection={snapshot.data.selection}
            waiting={snapshot.data.run.waiting_reason}
          />
        ) : null}
        <section className="run-plan" aria-label="План">
          <h4>
            Пункты плана{' '}
            {plan.data
              ? `${plan.data.items.filter((item) => item.status === 'done').length}/${plan.data.items.length}`
              : ''}
          </h4>
          {plan.isLoading ? (
            <p role="status">Загружаем план…</p>
          ) : plan.data?.items.length ? (
            <ol className="plan-progress">
              {plan.data.items.map((item) => (
                <li key={item.id} className="plan-item">
                  <strong>{item.title}</strong> · {item.status}
                  <p>{item.acceptance_criteria.join(' / ')}</p>
                  <p>
                    {item.evidence_ids.length} доказательств ·{' '}
                    {item.commit_shas.length} коммитов
                  </p>
                </li>
              ))}
            </ol>
          ) : plan.data ? (
            <p>План ещё не задан.</p>
          ) : null}
        </section>
        <section className="run-artifacts" aria-label="Артефакты">
          <h4>Артефакты</h4>
          <PageControls
            offset={artifactOffset}
            count={artifacts.data?.length ?? 0}
            busy={artifacts.isFetching}
            onChange={setArtifactOffset}
          />
          {artifacts.isLoading ? (
            <p role="status">Загружаем артефакты…</p>
          ) : artifacts.data?.length ? (
            <ul className="artifact-list">
              {artifacts.data.map((artifact) => (
                <Artifact key={artifact.id} artifact={artifact} />
              ))}
            </ul>
          ) : artifacts.data ? (
            <p>Артефактов пока нет.</p>
          ) : null}
        </section>
        <section className="run-command-journal" aria-label="Журнал команд">
          <h4>Журнал команд</h4>
          <PageControls
            offset={commandOffset}
            count={journal.data?.length ?? 0}
            busy={journal.isFetching}
            onChange={setCommandOffset}
          />
          {journal.isLoading ? (
            <p role="status">Загружаем журнал…</p>
          ) : journal.data?.length ? (
            <ol>
              {journal.data.map((entry) => (
                <li key={entry.command_id}>
                  <strong>{entry.command_id}</strong> · {entry.status} · seq{' '}
                  {entry.sequence} · {formatDateTime(entry.applied_at)}
                  {entry.response ? (
                    <pre>{JSON.stringify(entry.response, null, 2)}</pre>
                  ) : null}
                </li>
              ))}
            </ol>
          ) : journal.data ? (
            <p>Команд пока нет.</p>
          ) : null}
        </section>
      </div>
    </Modal>
  )
}

function Artifact({ artifact }: { artifact: ArtifactView }) {
  const [open, setOpen] = useState(false)
  return (
    <li className="profile-item">
      <strong>{artifact.schema_type}</strong>
      <span>
        {artifact.byte_length} байт · hash{' '}
        {shortHash(artifact.content_hash, 12)}
      </span>
      <button type="button" className="quiet" onClick={() => setOpen(!open)}>
        {open ? 'Скрыть' : 'Открыть'}
      </button>
      {open ? (
        <ArtifactDetail runId={artifact.run_id} artifactId={artifact.id} />
      ) : null}
    </li>
  )
}

function ResolutionForm({
  run,
  onSubmit,
  onClose,
}: {
  run: RunRecord
  onSubmit: (payload: Record<string, unknown>) => void
  onClose: () => void
}) {
  const [text, setText] = useState('')
  const [action, setAction] = useState('')
  const [error, setError] = useState('')
  const waiting =
    run.waiting_reason ??
    (run.runtime?.waiting_reason as RunRecord['waiting_reason'])
  const reason = waiting?.code ?? String(run.runtime?.waiting_code ?? '')
  return (
    <form
      aria-label="Решение ожидания"
      onSubmit={(event) => {
        event.preventDefault()
        try {
          onSubmit(resolutionPayload(run, text, action))
          setError('')
        } catch (error) {
          setError(describeRunError(error))
        }
      }}
    >
      <h4>Решение: {reason}</h4>
      <p>
        Сохранение решения не запускает выполнение. Затем используйте
        «Продолжить».
      </p>
      {reason === 'unknown_external_result' ? (
        <>
          <label htmlFor="resolution-action">Действие после сверки</label>
          <select
            id="resolution-action"
            value={action}
            onChange={(event) => setAction(event.target.value)}
            required
          >
            <option value="">Выберите действие</option>
            <option value="accept_result">
              Принять подтверждённый поздний результат
            </option>
            <option value="retry_authorized">
              Разрешить повтор операции после проверки её последствий
            </option>
          </select>
        </>
      ) : null}
      {['permission_required', 'invalid_response_format'].includes(reason) ? (
        <label>
          <input
            type="checkbox"
            checked={action === 'retry'}
            onChange={(event) => setAction(event.target.checked ? 'retry' : '')}
            required
          />
          Разрешаю новую попытку после устранения причины
        </label>
      ) : (
        <>
          <label htmlFor="resolution-text">
            {reason === 'limit_exceeded'
              ? `Новый лимит ${String(waiting?.details?.limit ?? '')}`
              : reason === 'unknown_external_result'
                ? 'Доказательства сверки'
                : 'Данные решения (JSON)'}
          </label>
          <textarea
            id="resolution-text"
            value={text}
            onChange={(event) => setText(event.target.value)}
            required
          />
        </>
      )}
      {error ? (
        <p role="alert" className="error">
          {describeRunError(error)}
        </p>
      ) : null}
      <footer>
        <button type="button" className="quiet" onClick={onClose}>
          Закрыть решение
        </button>
        <button type="submit">Сохранить решение</button>
      </footer>
    </form>
  )
}

function PageControls({
  offset,
  count,
  busy,
  onChange,
}: {
  offset: number
  count: number
  busy: boolean
  onChange: (value: number) => void
}) {
  return (
    <div className="page-controls">
      <button
        disabled={!offset || busy}
        onClick={() => onChange(Math.max(0, offset - 50))}
      >
        Предыдущие
      </button>
      <span>Страница {offset / 50 + 1}</span>
      <button
        disabled={count < 50 || busy}
        onClick={() => onChange(offset + 50)}
      >
        Следующие
      </button>
    </div>
  )
}
