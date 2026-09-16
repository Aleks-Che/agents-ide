import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  runsApi,
  describeRunError,
  type EventEnvelope,
  type HistoryCategory,
} from '../../api/runs'
import { useRunEventSource } from '../../app/useRunStream'
import {
  categories,
  eventPreview,
  matchesEvent,
  mergeEvents,
} from './observation'

export function RunTimeline({
  runId,
  nodeId,
  onNodeChange,
  nodes,
  onRefresh,
  onArtifact,
}: {
  runId: string
  nodeId: string
  onNodeChange: (id: string) => void
  nodes: string[]
  onRefresh: () => void
  onArtifact: (id: string) => void
}) {
  const client = useQueryClient()
  const [events, setEvents] = useState<EventEnvelope[]>([])
  const [category, setCategory] = useState<HistoryCategory>('all')
  const [history, setHistory] = useState(false)
  const filterKey = `${category}:${nodeId}`
  const [navigation, setNavigation] = useState<{
    key: string
    cursors: Array<number | undefined>
  }>({ key: filterKey, cursors: [undefined] })
  const cursors =
    navigation.key === filterKey ? navigation.cursors : [undefined]
  const setCursors = (
    next:
      | Array<number | undefined>
      | ((values: Array<number | undefined>) => Array<number | undefined>),
  ) => {
    setNavigation({
      key: filterKey,
      cursors: typeof next === 'function' ? next(cursors) : next,
    })
  }
  const [resetNotice, setResetNotice] = useState('')
  const [resetMinimum, setResetMinimum] = useState<number | null>(null)
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined,
  )
  const alive = useRef(true)
  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
      clearTimeout(refreshTimer.current)
    }
  }, [])
  const bootstrap = useQuery({
    queryKey: ['run_monitor', runId],
    queryFn: async () => {
      const snapshot = await runsApi.snapshot(runId)
      const page = await runsApi.history(runId, {
        before: snapshot.last_sequence + 1,
      })
      return { snapshot, page }
    },
    staleTime: Infinity,
    gcTime: 0,
    refetchOnWindowFocus: false,
    refetchInterval: (query) => (query.state.data ? false : 5000),
  })
  const stream = useRunEventSource({
    runId,
    enabled: Boolean(bootstrap.data),
    after: bootstrap.data?.snapshot.last_sequence ?? 0,
    onEvent: (event) => {
      setEvents((previous) => mergeEvents(previous, [event]))
      if (
        !refreshTimer.current &&
        ![
          'agent.message_delta',
          'attempt.text_delta',
          'attempt.progress',
        ].includes(event.type)
      ) {
        refreshTimer.current = setTimeout(() => {
          refreshTimer.current = undefined
          onRefresh()
        }, 300)
      }
    },
    onReset: (reason) => {
      setResetNotice(reason)
      setEvents([])
    },
    onSnapshot: async (snapshot) => {
      const page = await runsApi.history(runId, {
        before: snapshot.last_sequence + 1,
      })
      if (!alive.current) return
      client.setQueryData(['run_snapshot', runId], snapshot)
      setEvents(mergeEvents([], page.events))
      setResetMinimum(page.min_retained_sequence)
      void client.invalidateQueries({ queryKey: ['run_history', runId] })
      onRefresh()
    },
    onStatus: (next) => {
      if (next.state === 'auth_expired')
        void client.invalidateQueries({ queryKey: ['session'] })
    },
    onFinalState: onRefresh,
  })
  const page = useQuery({
    queryKey: ['run_history', runId, category, nodeId, cursors.at(-1)],
    queryFn: () =>
      runsApi.history(runId, { before: cursors.at(-1), category, nodeId }),
    enabled: history,
    gcTime: 0,
  })
  const live = mergeEvents(
    resetNotice ? [] : (bootstrap.data?.page.events ?? []),
    events,
  )
  const shown = history
    ? (page.data?.events ?? [])
    : live.filter((event) => matchesEvent(event, category, nodeId))
  const minimum =
    page.data?.min_retained_sequence ??
    resetMinimum ??
    bootstrap.data?.page.min_retained_sequence ??
    0
  return (
    <section className="run-events" aria-label="События">
      <h4>История выполнения</h4>
      <p role="status">
        Поток: {stream.state} · курсор {stream.lastSequence}
      </p>
      {['connecting', 'reset_required'].includes(stream.state) ? (
        <p role="status">
          Связь с API восстанавливается. Состояние Run показано по последним
          данным сервера; выполнение может продолжаться.
        </p>
      ) : null}
      {stream.state === 'auth_expired' ? (
        <p role="alert">
          Сессия истекла. Подключитесь повторно для обновления данных.
        </p>
      ) : null}
      {resetNotice || minimum > 1 ? (
        <p role="status">
          Курсор недоступен: состояние обновлено. История доступна с #{minimum};
          удалённые сервером события недоступны.{' '}
          {resetNotice === 'slow_consumer'
            ? 'Буфер потока переполнен, загружены последние события.'
            : ''}
        </p>
      ) : null}
      {bootstrap.error ? (
        <p role="alert">
          {describeRunError(bootstrap.error)}{' '}
          <button onClick={() => void bootstrap.refetch()}>
            Повторить подключение
          </button>
        </p>
      ) : null}
      <div className="observation-filters">
        <label>
          Фильтр событий
          <select
            value={category}
            onChange={(event) => {
              setCategory(event.target.value as HistoryCategory)
              setCursors([undefined])
            }}
          >
            {Object.entries(categories).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Узел истории
          <select
            value={nodeId}
            onChange={(event) => {
              onNodeChange(event.target.value)
              setCursors([undefined])
            }}
          >
            <option value="">Все узлы</option>
            {nodes.map((id) => (
              <option key={id}>{id}</option>
            ))}
          </select>
        </label>
        <button
          onClick={() => {
            setHistory(!history)
            setCursors([undefined])
          }}
        >
          {history ? 'Вернуться к потоку' : 'Открыть сохранённую историю'}
        </button>
      </div>
      <p className="hint">
        {history
          ? 'Сохранённая история, до 200 событий на странице.'
          : 'Последние события, до 400 записей / 512 КиБ. Более ранние сообщения и переключения — в сохранённой истории.'}{' '}
        Новые события сверху. Фильтры истории применяются на сервере.
      </p>
      {history ? (
        <>
          <button
            disabled={cursors.length < 2 || page.isFetching}
            onClick={() => setCursors((values) => values.slice(0, -1))}
          >
            Новее
          </button>{' '}
          <button
            disabled={!page.data?.has_more || page.isFetching}
            onClick={() =>
              setCursors((values) => [...values, page.data!.next_before!])
            }
          >
            Старее
          </button>{' '}
          <button
            disabled={page.isFetching}
            onClick={() => {
              setCursors([undefined])
              void page.refetch()
            }}
          >
            Обновить историю
          </button>
          {page.error ? (
            <p role="alert">{describeRunError(page.error)}</p>
          ) : null}
          {page.isFetching ? <p role="status">Загружаем историю…</p> : null}
        </>
      ) : null}
      <VirtualEvents
        key={`${history}-${category}-${nodeId}-${cursors.at(-1)}`}
        events={shown}
        onArtifact={onArtifact}
      />
    </section>
  )
}

const ROW_HEIGHT = 76
const VIEW_HEIGHT = 380
function VirtualEvents({
  events,
  onArtifact,
}: {
  events: EventEnvelope[]
  onArtifact: (id: string) => void
}) {
  const [top, setTop] = useState(0)
  const [selected, setSelected] = useState<EventEnvelope | null>(null)
  const start = Math.min(
    Math.max(0, Math.floor(top / ROW_HEIGHT) - 3),
    Math.max(0, events.length - 1),
  )
  const end = Math.min(
    events.length,
    start + Math.ceil(VIEW_HEIGHT / ROW_HEIGHT) + 7,
  )
  return (
    <>
      <div
        className="timeline-viewport"
        tabIndex={0}
        aria-label="Лента событий"
        onScroll={(event) => setTop(event.currentTarget.scrollTop)}
      >
        <ol
          className="timeline-rows"
          style={{ height: events.length * ROW_HEIGHT }}
        >
          {events.slice(start, end).map((event, index) => (
            <li
              key={event.sequence}
              style={{ top: (start + index) * ROW_HEIGHT, height: ROW_HEIGHT }}
            >
              <button
                onClick={() => setSelected(event)}
                aria-label={`Событие ${event.sequence}: ${event.type}`}
              >
                <strong>
                  #{event.sequence} · {event.type}
                </strong>
                <span>
                  {new Date(event.occurred_at * 1000).toLocaleTimeString(
                    'ru-RU',
                  )}{' '}
                  · {event.node_id ?? 'Run'} ·{' '}
                  {event.step_attempt_id?.slice(0, 8) ?? '—'}
                </span>
                <span>{eventPreview(event) || 'Открыть подробности'}</span>
              </button>
            </li>
          ))}
        </ol>
        {!events.length ? <p>Событий по выбранному фильтру нет.</p> : null}
      </div>
      <p className="hint">
        Загружено событий: {events.length}. Время — локальное.
      </p>
      {selected ? (
        <section aria-label="Подробности события">
          <h5>
            #{selected.sequence} · {selected.type}
          </h5>
          <p>
            Узел {selected.node_id ?? '—'} · выполнение{' '}
            {selected.step_execution_id ?? '—'} · попытка{' '}
            {selected.step_attempt_id ?? '—'}
          </p>
          <pre>{JSON.stringify(selected.payload, null, 2)}</pre>
          {Object.entries(selected.payload)
            .filter(
              ([key, value]) =>
                /(_ref|artifact_id)$/.test(key) &&
                typeof value === 'string' &&
                /^[a-f0-9]{32}$/i.test(value),
            )
            .map(([key, id]) => (
              <button key={key} onClick={() => onArtifact(String(id))}>
                Артефакт: {key}
              </button>
            ))}
          <button onClick={() => setSelected(null)}>Скрыть событие</button>
        </section>
      ) : null}
    </>
  )
}
