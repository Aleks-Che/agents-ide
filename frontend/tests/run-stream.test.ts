import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { connectRunStream } from '../src/app/useRunStream'
import {
  allowedCommands,
  pendingAgentRecovery,
  resolutionPayload,
} from '../src/features/runs/controls'
import type { RunRecord } from '../src/api/runs'
import {
  graphRoles,
  parseLimitOverrides,
  serialiseSelection,
} from '../src/features/bindings/utils'

class FakeSource extends EventTarget {
  static instances: FakeSource[] = []
  closed = false
  constructor(public url: string) {
    super()
    FakeSource.instances.push(this)
  }
  close() {
    this.closed = true
  }
  emit(type: string, data: unknown = {}) {
    this.dispatchEvent(new MessageEvent(type, { data: JSON.stringify(data) }))
  }
}
const event = (sequence: number) => ({
  run_id: 'r1',
  sequence,
  type: 'run.state_changed',
  payload: {},
})
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status })
let dispose: (() => void) | undefined
beforeEach(() => {
  vi.useFakeTimers()
  FakeSource.instances = []
  vi.stubGlobal('EventSource', FakeSource)
})
afterEach(() => {
  dispose?.()
  dispose = undefined
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('Run event transport', () => {
  it('closes a live connection on browser offline and removes the listener on disposal', async () => {
    const browser = new EventTarget()
    vi.stubGlobal('window', browser)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({})))
    const onStatus = vi.fn()
    dispose = connectRunStream({
      runId: 'r1',
      after: 17,
      onEvent: vi.fn(),
      onStatus,
    })
    browser.dispatchEvent(new Event('offline'))
    expect(FakeSource.instances[0].closed).toBe(true)
    expect(onStatus).toHaveBeenLastCalledWith(
      expect.objectContaining({ state: 'connecting', lastSequence: 17 }),
    )
    await vi.advanceTimersByTimeAsync(500)
    expect(FakeSource.instances[1].url).toMatch(/after=17$/)
    dispose()
    browser.dispatchEvent(new Event('offline'))
    await vi.advanceTimersByTimeAsync(10000)
    expect(FakeSource.instances).toHaveLength(2)
  })
  it('keeps one stream, drops duplicates and reconnects from the last received cursor', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({})))
    const onEvent = vi.fn(),
      onStatus = vi.fn()
    dispose = connectRunStream({ runId: 'r1', after: 0, onEvent, onStatus })
    const source = FakeSource.instances[0]
    source.emit('run.event', event(1))
    source.emit('run.event', event(2))
    source.emit('run.event', event(1))
    expect(onEvent).toHaveBeenCalledTimes(2)
    expect(FakeSource.instances).toHaveLength(1)
    source.emit('error')
    await vi.advanceTimersByTimeAsync(500)
    expect(FakeSource.instances[1].url).toMatch(/after=2$/)
    source.emit('run.event', event(3))
    expect(onEvent).toHaveBeenCalledTimes(2)
    dispose()
    await vi.advanceTimersByTimeAsync(10000)
    expect(FakeSource.instances).toHaveLength(2)
  })
  it('resets to a snapshot cursor without replaying unbounded history or trusting a supplied URL', async () => {
    const snapshot = { last_sequence: 90000, min_retained_sequence: 70000 }
    const fetch = vi.fn().mockResolvedValueOnce(json(snapshot))
    vi.stubGlobal('fetch', fetch)
    const onEvent = vi.fn(),
      onStatus = vi.fn(),
      onReset = vi.fn(),
      onSnapshot = vi.fn()
    dispose = connectRunStream({
      runId: 'r1',
      after: 2,
      onEvent,
      onStatus,
      onReset,
      onSnapshot,
    })
    FakeSource.instances[0].emit('stream.reset_required', {
      reason: 'slow_consumer',
      snapshot_url: 'https://untrusted.invalid/',
    })
    await vi.advanceTimersByTimeAsync(0)
    expect(onStatus).toHaveBeenCalledWith(
      expect.objectContaining({ state: 'reset_required' }),
    )
    expect(onReset).toHaveBeenCalledOnce()
    expect(onSnapshot).toHaveBeenCalledWith(snapshot)
    expect(fetch.mock.calls.map((args) => args[0])).toEqual([
      '/api/runs/r1/snapshot',
    ])
    expect(onEvent).not.toHaveBeenCalled()
    expect(FakeSource.instances[1].url).toMatch(/after=90000$/)
    FakeSource.instances[1].emit('run.event', event(90001))
    FakeSource.instances[1].emit('run.event', event(90001))
    expect(onEvent).toHaveBeenCalledOnce()
  })
  it('recovers after a prolonged API outage with bounded backoff and the original cursor', async () => {
    const fetch = vi.fn().mockRejectedValue(new Error('offline'))
    vi.stubGlobal('fetch', fetch)
    const onEvent = vi.fn()
    dispose = connectRunStream({ runId: 'r1', after: 42, onEvent })
    FakeSource.instances[0].emit('error')
    await vi.advanceTimersByTimeAsync(5 * 60 * 1000)
    expect(fetch.mock.calls.length).toBeLessThan(40)
    expect(FakeSource.instances).toHaveLength(1)
    fetch.mockResolvedValue(json({}))
    await vi.advanceTimersByTimeAsync(10000)
    expect(FakeSource.instances[1].url).toMatch(/after=42$/)
    FakeSource.instances[1].emit('run.event', event(43))
    expect(onEvent).toHaveBeenCalledOnce()
  })
  it('replaces cached history after compaction and resumes at the snapshot cursor', async () => {
    const snapshot = { last_sequence: 1001 }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(snapshot)))
    const onEvent = vi.fn(),
      onSnapshot = vi.fn(),
      onReset = vi.fn()
    dispose = connectRunStream({
      runId: 'r1',
      after: 1000,
      onEvent,
      onSnapshot,
      onReset,
    })
    const source = FakeSource.instances[0]
    source.emit('run.event', {
      ...event(1001),
      type: 'run.history_compacted',
      run_id: 'foreign',
    })
    expect(onReset).not.toHaveBeenCalled()
    source.emit('run.event', { ...event(1001), type: 'run.history_compacted' })
    await vi.advanceTimersByTimeAsync(0)
    expect(onReset).toHaveBeenCalledWith('history_compacted')
    expect(onSnapshot).toHaveBeenCalledWith(snapshot)
    expect(onEvent).not.toHaveBeenCalled()
    expect(FakeSource.instances[1].url).toMatch(/after=1001$/)
  })
  it('does not revive a disposed connection after an in-flight reset', async () => {
    let resolve!: (value: Response) => void
    vi.stubGlobal(
      'fetch',
      vi.fn().mockReturnValue(
        new Promise<Response>((done) => {
          resolve = done
        }),
      ),
    )
    const onSnapshot = vi.fn()
    dispose = connectRunStream({
      runId: 'r1',
      after: 1,
      onEvent: vi.fn(),
      onSnapshot,
    })
    FakeSource.instances[0].emit('stream.reset_required')
    dispose()
    resolve(json({ last_sequence: 99 }))
    await vi.advanceTimersByTimeAsync(10000)
    expect(onSnapshot).not.toHaveBeenCalled()
    expect(FakeSource.instances).toHaveLength(1)
  })
  it('exposes explicit session expiration and closes the source', () => {
    const onStatus = vi.fn()
    dispose = connectRunStream({
      runId: 'r1',
      after: 4,
      onEvent: vi.fn(),
      onStatus,
    })
    FakeSource.instances[0].emit('auth.expired')
    expect(onStatus).toHaveBeenLastCalledWith(
      expect.objectContaining({ state: 'auth_expired', lastSequence: 4 }),
    )
    expect(FakeSource.instances[0].closed).toBe(true)
  })
  it('recognises HTTP 401 on a failed connection instead of reconnecting forever', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          json({ code: 'auth_required', message: 'expired', details: {} }, 401),
        ),
    )
    const onStatus = vi.fn()
    dispose = connectRunStream({
      runId: 'r1',
      after: 0,
      onEvent: vi.fn(),
      onStatus,
    })
    FakeSource.instances[0].emit('error')
    await vi.advanceTimersByTimeAsync(20000)
    expect(FakeSource.instances).toHaveLength(1)
    expect(onStatus).toHaveBeenLastCalledWith(
      expect.objectContaining({ state: 'auth_expired' }),
    )
  })
  it('does not reconnect after a terminal stream', async () => {
    const onFinalState = vi.fn()
    dispose = connectRunStream({
      runId: 'r1',
      after: 0,
      onEvent: vi.fn(),
      onFinalState,
    })
    FakeSource.instances[0].emit('stream.closed', { final_state: 'completed' })
    FakeSource.instances[0].emit('error')
    await vi.advanceTimersByTimeAsync(20000)
    expect(onFinalState).toHaveBeenCalledWith('completed')
    expect(FakeSource.instances).toHaveLength(1)
  })
})

describe('Run controls and binding drafts', () => {
  it('matches recovery and waiting action restrictions', () => {
    expect(allowedCommands({ state: 'recovering' } as RunRecord)).toEqual([
      'stop',
      'cancel',
    ])
    expect(allowedCommands({ state: 'paused' } as RunRecord)).not.toContain(
      'resolve',
    )
    expect(
      allowedCommands({
        state: 'waiting_input',
        waiting_reason: { allowed_actions: ['resolve', 'cancel'] },
      } as RunRecord),
    ).toEqual(['cancel', 'resolve'])
  })
  it('shows saved continuation only while its attempt awaits resume', () => {
    const run = {
      state: 'waiting_input',
      waiting_reason: { details: { attempt_id: 'current' } },
      runtime: {
        agent_recovery_attempts: { current: 'continue_session' },
        pending_agent_recovery: {
          attempt_id: 'current',
          execution_id: 'execution',
          action: 'continue_session',
        },
      },
    } as unknown as RunRecord
    expect(pendingAgentRecovery(run)).toBe(true)
    expect(pendingAgentRecovery({ ...run, state: 'queued' })).toBe(false)
    expect(pendingAgentRecovery({ ...run, runtime: {} })).toBe(false)
    expect(
      pendingAgentRecovery({
        ...run,
        waiting_reason: {
          ...run.waiting_reason!,
          details: { attempt_id: 'later' },
        },
      }),
    ).toBe(false)
    // A failed continuation can retain the same attempt ID. Its audit record is
    // not an unconsumed decision and must not display "ready to continue".
    expect(
      pendingAgentRecovery({
        ...run,
        runtime: { agent_recovery_attempts: { current: 'continue_session' } },
      }),
    ).toBe(false)
  })
  it('sends explicit limits and rejects empty or unconfirmed resolutions', () => {
    const run = {
      state: 'waiting_input',
      waiting_reason: {
        code: 'limit_exceeded',
        details: { limit: 'max_calls' },
        allowed_actions: ['resolve'],
      },
    } as unknown as RunRecord
    expect(resolutionPayload(run, '250', '')).toEqual({
      limit_overrides: { max_calls: 250 },
    })
    expect(() => resolutionPayload(run, '', '')).toThrow()
    expect(() =>
      resolutionPayload(
        { waiting_reason: { code: 'permission_required' } } as RunRecord,
        '',
        '',
      ),
    ).toThrow()
    expect(() => resolutionPayload({} as RunRecord, '{}', '')).toThrow()
  })
  it('reprocesses saved JSON without authorizing another model request', () => {
    const run = {
      waiting_reason: { code: 'invalid_response_format' },
    } as RunRecord
    expect(
      resolutionPayload(run, '', 'reprocess', { extract_json: true }),
    ).toEqual({
      json_processing: { extract_json: true },
    })
    expect(() => resolutionPayload(run, '', 'reprocess', {})).toThrow()
  })
  it.each(['continue_session', 'next_candidate'])(
    'continues the saved agent attempt with %s without replay authorization',
    (action) => {
      const run = {
        waiting_reason: {
          code: 'unknown_external_result',
          resolution_schema: {
            agent_recovery: {
              attempt_id: 'saved-attempt',
              actions: ['continue_session', 'next_candidate'],
            },
          },
        },
      } as unknown as RunRecord
      expect(resolutionPayload(run, '', action)).toEqual({
        agent_recovery: { attempt_id: 'saved-attempt', action },
      })
      expect(() => resolutionPayload({} as RunRecord, '', action)).toThrow()
    },
  )
  it.each([
    'broken',
    '[]',
    '{"max_calls": -1}',
    '{"max_calls": 1.5}',
    '{"max_calls":10001}',
    '{"tokens":1}',
  ])('rejects invalid limit input %s without clearing saved limits', (input) =>
    expect(() => parseLimitOverrides(input)).toThrow(),
  )
  it('distinguishes inheritance from an incomplete direct selection', () => {
    expect(serialiseSelection({ kind: null }, 'agent')).toBeNull()
    expect(() =>
      serialiseSelection({ kind: 'direct', model_id: 'm' }, 'llm'),
    ).toThrow()
    expect(() => serialiseSelection({ kind: 'group' }, 'agent')).toThrow()
    expect(
      graphRoles({
        nodes: [{ type: 'LLMRequest', config: { role: 'review' } }],
      }),
    ).toEqual({ review: { kind: 'llm' } })
  })
})
