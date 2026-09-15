import { afterEach, expect, it, vi } from 'vitest'
import { bindingsApi } from '../src/api/bindings'
import { ApiError } from '../src/api/client'
import {
  launchParameters,
  uncertainStart,
  loadPendingStart,
  pendingStartKey,
} from '../src/features/runs/launch'

afterEach(() => vi.unstubAllGlobals())

it('preflight sends the same execution mode, inputs and overrides as start', async () => {
  const fetch = vi.fn().mockResolvedValue(
    new Response(
      JSON.stringify({
        ok: true,
        errors: [],
        warnings: [],
        execution_hash: 'hash',
        requires_trust: true,
        candidates: { node: [] },
      }),
    ),
  )
  vi.stubGlobal('fetch', fetch)
  const parameters = launchParameters(
    'simulated',
    '{"task":"review"}',
    '{"max_calls":20}',
    '["test"]',
  )
  const result = await bindingsApi.preflight('binding', 'csrf', parameters)
  expect(result.preview).toEqual({
    requires_trust: true,
    candidates: { node: [] },
  })
  expect(result.execution_hash).toBe('hash')
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
    execution_mode: 'simulated',
    inputs: { task: 'review' },
    overrides: { limit_overrides: { max_calls: 20 }, command_filter: ['test'] },
  })
  expect(fetch.mock.calls[0][1].headers.get('X-CSRF-Token')).toBe('csrf')
})

it.each([
  ['[]', '{}', 'null'],
  ['null', '{}', 'null'],
  ['bad', '{}', 'null'],
  ['{}', '{"max_calls":0}', 'null'],
  ['{}', '{}', '{}'],
  ['{}', '{}', '["x","x"]'],
  ['{}', '{}', '[1]'],
])('rejects invalid launch values %s %s %s', (inputs, limits, commands) => {
  expect(() => launchParameters('real', inputs, limits, commands)).toThrow()
})

it('distinguishes inheriting a command filter from disabling all commands', () => {
  expect(
    launchParameters('real', '{}', '{}', 'null').overrides,
  ).not.toHaveProperty('command_filter')
  expect(
    launchParameters('real', '{}', '{}', '[]').overrides?.command_filter,
  ).toEqual([])
})

it('restores the exact pending payload per chat without creating an idempotency key', () => {
  const body = {
    project_id: 'p',
    chat_id: 'c',
    binding_id: 'b',
    idempotency_key: 'original',
    inputs: { task: 'draft' },
  }
  const store = new Map([[pendingStartKey('c'), JSON.stringify(body)]])
  vi.stubGlobal('sessionStorage', {
    getItem: (key: string) => store.get(key) ?? null,
  })
  expect(loadPendingStart('p', 'c')).toEqual(body)
  expect(loadPendingStart('p', 'another')).toBeNull()
  expect(() => loadPendingStart('other-project', 'c')).toThrow()
})

it('retains an uncertain start on network, timeout and server errors', () => {
  const error = (status: number) =>
    new ApiError(status, {
      code: 'test',
      message: 'test',
      details: {},
      request_id: '',
      retryable: true,
    })
  expect(uncertainStart(new TypeError('network'))).toBe(true)
  expect(uncertainStart(error(500))).toBe(true)
  expect(uncertainStart(error(408))).toBe(true)
  // Auth/rate-limit middleware can reject a retry before checking an already committed key.
  expect(uncertainStart(error(401))).toBe(true)
  expect(uncertainStart(error(403))).toBe(true)
  expect(uncertainStart(error(429))).toBe(true)
  expect(uncertainStart(error(422))).toBe(false)
})
