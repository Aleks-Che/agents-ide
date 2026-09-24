import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../src/api/client'
import { describeRunError, runsApi } from '../src/api/runs'

afterEach(() => vi.unstubAllGlobals())

function stubJsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function captureFetch(): ReturnType<typeof vi.fn> {
  const fetch = vi.fn()
  vi.stubGlobal('fetch', fetch)
  return fetch
}

describe('runsApi', () => {
  it('lists runs for a project', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(stubJsonResponse(200, []))
    await runsApi.list({ projectId: 'p1' })
    expect(fetch.mock.calls[0][0]).toBe('/api/runs?project_id=p1')
  })

  it('lists runs for a chat filter', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(stubJsonResponse(200, []))
    await runsApi.list({ projectId: 'p1', chatId: 'c1' })
    expect(fetch.mock.calls[0][0]).toBe('/api/runs?project_id=p1&chat_id=c1')
  })

  it('lists runs using chat filter only', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(stubJsonResponse(200, []))
    await runsApi.list({ chatId: 'c1' })
    expect(fetch.mock.calls[0][0]).toBe('/api/runs?chat_id=c1')
  })

  it('submits commands with expected_state_version and CSRF', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(
      stubJsonResponse(200, { command_id: 'c1', status: 'accepted' }),
    )
    await runsApi.submitCommand(
      'r1',
      {
        command_id: 'c1',
        command_type: 'pause',
        expected_state_version: 2,
      },
      'csrf-r',
    )
    const [, init] = fetch.mock.calls[0]
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({
      command_id: 'c1',
      command_type: 'pause',
      expected_state_version: 2,
    })
    expect(init.headers.get('X-CSRF-Token')).toBe('csrf-r')
  })

  it('loads plan summary for a run', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(
      stubJsonResponse(200, { plan: null, summary: {}, items: [] }),
    )
    await runsApi.plan('r1')
    expect(fetch.mock.calls[0][0]).toBe('/api/runs/r1/plan')
  })

  it('loads diagnostics', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(
      stubJsonResponse(200, {
        run_id: 'r1',
        state: 'running',
        state_version: 1,
        processes: [],
      }),
    )
    await runsApi.diagnostics('r1')
    expect(fetch.mock.calls[0][0]).toBe('/api/runs/r1/diagnostics')
  })

  it('lists artifacts and loads one by id', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(stubJsonResponse(200, []))
    await runsApi.artifacts('r1')
    expect(fetch.mock.calls[0][0]).toBe(
      '/api/runs/r1/artifacts?offset=0&limit=50',
    )
    fetch.mockResolvedValueOnce(stubJsonResponse(200, { id: 'a1' }))
    await runsApi.artifact('r1', 'a1')
    expect(fetch.mock.calls[1][0]).toBe('/api/runs/r1/artifacts/a1')
  })

  it('paginates events with after and limit', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(stubJsonResponse(200, { events: [] }))
    await runsApi.events('r1', { after: 5, limit: 50 })
    expect(fetch.mock.calls[0][0]).toBe('/api/runs/r1/events?after=5&limit=50')
  })
})

describe('describeRunError', () => {
  it('extracts api error message', () => {
    const error = new ApiError(409, {
      code: 'version_conflict',
      message: 'Конфликт версии',
      details: {},
      request_id: 'req',
      retryable: false,
    })
    expect(describeRunError(error)).toBe('Конфликт версии')
  })

  it('falls back to default for unknown input', () => {
    expect(describeRunError(null)).toBe('Неизвестная ошибка')
  })
})

describe('restart catalog recovery', () => {
  const body = { command_id: 'same-command', expected_state_version: 7 }
  const failure = {
    code: 'graph_validation_failed',
    message: 'Граф не прошёл preflight',
    details: {
      errors: ['first', 'second'].map((node_id) => ({
        code: 'harness_catalog_unverified',
        node_id,
        message: 'Refresh',
        details: { harness_profile_id: 'h1' },
      })),
    },
  }

  it('refreshes a shared harness once and retries the exact restart command', async () => {
    const fetch = captureFetch()
    fetch
      .mockResolvedValueOnce(stubJsonResponse(422, failure))
      .mockResolvedValueOnce(stubJsonResponse(200, { status: 'fresh' }))
      .mockResolvedValueOnce(stubJsonResponse(201, { id: 'replacement' }))
    expect(await runsApi.restart('r1', body, 'csrf')).toEqual({
      id: 'replacement',
    })
    expect(fetch.mock.calls.map(([url]) => url)).toEqual([
      '/api/runs/r1/restart',
      '/api/harness_profiles/h1/models/refresh?force=true',
      '/api/runs/r1/restart',
    ])
    expect(JSON.parse(fetch.mock.calls[2][1].body)).toEqual(body)
    expect(fetch.mock.calls[0][1].body).toBe(fetch.mock.calls[2][1].body)
    expect(fetch.mock.calls[1][1].headers.get('X-CSRF-Token')).toBe('csrf')
  })

  it('does not loop when refresh leaves the model unverified', async () => {
    const fetch = captureFetch()
    fetch
      .mockResolvedValueOnce(stubJsonResponse(422, failure))
      .mockResolvedValueOnce(stubJsonResponse(200, { status: 'fresh' }))
      .mockResolvedValueOnce(stubJsonResponse(422, failure))
    await expect(runsApi.restart('r1', body, 'csrf')).rejects.toMatchObject({
      body: failure,
    })
    expect(fetch).toHaveBeenCalledTimes(3)
  })

  it.each([
    [500, failure],
    [409, { code: 'version_conflict', message: 'Conflict', details: {} }],
    [
      422,
      {
        ...failure,
        details: { errors: [{ code: 'harness_model_unavailable' }] },
      },
    ],
  ])(
    'does not refresh or retry unrelated failure %s',
    async (status, error) => {
      const fetch = captureFetch()
      fetch.mockResolvedValueOnce(stubJsonResponse(status, error))
      await expect(runsApi.restart('r1', body, 'csrf')).rejects.toBeInstanceOf(
        ApiError,
      )
      expect(fetch).toHaveBeenCalledTimes(1)
    },
  )

  it('does not restart when the catalog refresh fails', async () => {
    const fetch = captureFetch()
    fetch
      .mockResolvedValueOnce(stubJsonResponse(422, failure))
      .mockResolvedValueOnce(
        stubJsonResponse(422, {
          code: 'harness_catalog_unavailable',
          message: 'Cannot load models',
          details: {},
        }),
      )
    await expect(runsApi.restart('r1', body, 'csrf')).rejects.toThrow(
      'Cannot load models',
    )
    expect(fetch).toHaveBeenCalledTimes(2)
  })
})
