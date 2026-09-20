import { useEffect, useRef, useState } from 'react'
import { ApiError, request } from '../api/client'
import { runsApi, type EventEnvelope, type RunSnapshot } from '../api/runs'

export type SseStreamState =
  'idle' | 'connecting' | 'open' | 'closed' | 'reset_required' | 'auth_expired'
export interface SseStatus {
  state: SseStreamState
  lastSequence: number
  resetReason?: string
}
export interface UseRunEventSourceOptions {
  runId: string
  enabled: boolean
  after: number
  onEvent: (event: EventEnvelope) => void
  onStatus?: (status: SseStatus) => void
  onReset?: (reason: string) => void
  onSnapshot?: (snapshot: RunSnapshot) => void | Promise<void>
  onFinalState?: (state: string) => void
}

// The cursor belongs to the connection, never to a render dependency.
// Explicit reconnect also handles HTTP 401, which EventSource hides from JS.
export function connectRunStream(
  options: Omit<UseRunEventSourceOptions, 'enabled'>,
): () => void {
  let disposed = false
  let ended = false
  let source: EventSource | undefined
  let timer: ReturnType<typeof setTimeout> | undefined
  let sequence = options.after
  let delay = 500
  const status = (state: SseStreamState, resetReason?: string) => {
    if (!disposed)
      options.onStatus?.({ state, lastSequence: sequence, resetReason })
  }
  const accept = (event: EventEnvelope) => {
    if (
      disposed ||
      event.run_id !== options.runId ||
      !Number.isSafeInteger(event.sequence) ||
      event.sequence <= sequence
    )
      return
    sequence = event.sequence
    options.onEvent(event)
  }
  const failed = (error: unknown) => {
    if (disposed) return
    clearTimeout(timer)
    if (error instanceof ApiError && error.status === 401) {
      ended = true
      status('auth_expired')
      return
    }
    status('connecting')
    timer = setTimeout(() => void reconnect(), delay)
    delay = Math.min(delay * 2, 10_000)
  }
  const reconnect = async () => {
    try {
      await request('/auth/session')
      if (!disposed) open()
    } catch (error) {
      failed(error)
    }
  }
  const reset = async (reason: string) => {
    source?.close()
    status('reset_required', reason)
    options.onReset?.(reason)
    try {
      // Jump to an atomic snapshot, then fetch only a bounded tail for display.
      // Old history stays available through REST pagination, even for a slow client.
      const snapshot = await runsApi.snapshot(options.runId)
      if (disposed) return
      await options.onSnapshot?.(snapshot)
      if (disposed) return
      sequence = snapshot.last_sequence
      if (!disposed) open()
    } catch (error) {
      failed(error)
    }
  }
  const open = () => {
    if (disposed) return
    source?.close()
    status('connecting')
    const current = new EventSource(
      `/api/runs/${encodeURIComponent(options.runId)}/stream?after=${sequence}`,
      { withCredentials: true },
    )
    source = current
    const active = () => !disposed && source === current
    current.addEventListener('open', () => {
      if (active()) {
        delay = 500
        status('open')
      }
    })
    current.addEventListener('run.event', (message) => {
      if (!active()) return
      try {
        const event = JSON.parse(
          (message as MessageEvent).data,
        ) as EventEnvelope
        if (
          event.type === 'run.history_compacted' &&
          event.run_id === options.runId &&
          Number.isSafeInteger(event.sequence) &&
          event.sequence > sequence
        ) {
          current.close()
          source = undefined
          void reset('history_compacted')
          return
        }
        accept(event)
        status('open')
      } catch {
        current.close()
        source = undefined
        failed(new Error('Invalid event'))
      }
    })
    current.addEventListener('error', () => {
      if (!active()) return
      current.close()
      source = undefined
      failed(new Error('Stream disconnected'))
    })
    current.addEventListener('stream.reset_required', (message) => {
      if (!active()) return
      current.close()
      source = undefined
      let reason = 'cursor_unavailable'
      try {
        reason = JSON.parse((message as MessageEvent).data).reason ?? reason
      } catch {
        /* use default */
      }
      void reset(reason)
    })
    current.addEventListener('auth.expired', () => {
      if (!active()) return
      current.close()
      source = undefined
      ended = true
      status('auth_expired')
    })
    current.addEventListener('stream.closed', (message) => {
      if (!active()) return
      current.close()
      source = undefined
      ended = true
      status('closed')
      try {
        const payload = JSON.parse((message as MessageEvent).data)
        if (payload.final_state) options.onFinalState?.(payload.final_state)
      } catch {
        /* state is also fetched via REST */
      }
    })
  }
  const offline = () => {
    if (disposed || ended) return
    source?.close()
    source = undefined
    failed(new Error('Browser offline'))
  }
  if (typeof window !== 'undefined') window.addEventListener('offline', offline)
  open()
  return () => {
    disposed = true
    source?.close()
    clearTimeout(timer)
    if (typeof window !== 'undefined')
      window.removeEventListener('offline', offline)
  }
}

export function useRunEventSource(
  options: UseRunEventSourceOptions,
): SseStatus {
  const { runId, enabled, after } = options
  const [status, setStatus] = useState<SseStatus>({
    state: 'idle',
    lastSequence: after,
  })
  const handlers = useRef(options)
  useEffect(() => {
    handlers.current = options
  }, [options])
  useEffect(() => {
    if (!enabled || !runId) return
    return connectRunStream({
      runId,
      after,
      onEvent: (event) => handlers.current.onEvent(event),
      onStatus: (next) => {
        setStatus(next)
        handlers.current.onStatus?.(next)
      },
      onReset: (reason) => handlers.current.onReset?.(reason),
      onSnapshot: (snapshot) => handlers.current.onSnapshot?.(snapshot),
      onFinalState: (state) => handlers.current.onFinalState?.(state),
    })
  }, [runId, enabled, after])
  return status
}
