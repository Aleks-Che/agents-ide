import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { connectRunStream } from '../src/app/useRunStream'
import {
  allowedCommands,
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
  it('refreshes snapshot and paginates retained history on reset, without accepting a supplied URL', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json({}))
      .mockResolvedValueOnce(
        json({ events: [event(7)], min_retained_sequence: 7, has_more: true }),
      )
      .mockResolvedValueOnce(json({ events: [event(8)], has_more: false }))
    vi.stubGlobal('fetch', fetch)
    const onEvent = vi.fn(),
      onStatus = vi.fn(),
      onReset = vi.fn()
    dispose = connectRunStream({
      runId: 'r1',
      after: 2,
      onEvent,
      onStatus,
      onReset,
    })
    FakeSource.instances[0].emit('stream.reset_required', {
      reason: 'cursor_unavailable',
      snapshot_url: 'https://untrusted.invalid/',
    })
    await vi.advanceTimersByTimeAsync(0)
    expect(onStatus).toHaveBeenCalledWith(
      expect.objectContaining({ state: 'reset_required' }),
    )
    expect(onReset).toHaveBeenCalledOnce()
    expect(fetch.mock.calls.map((args) => args[0])).toEqual([
      '/api/runs/r1/snapshot',
      '/api/runs/r1/events/replay',
      '/api/runs/r1/events?after=7',
    ])
    expect(onEvent.mock.calls.map((args) => args[0].sequence)).toEqual([7, 8])
    expect(FakeSource.instances[1].url).toMatch(/after=8$/)
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
