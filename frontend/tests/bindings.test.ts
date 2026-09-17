import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../src/api/client'
import {
  bindingsApi,
  describeBindingError,
  presetsApi,
  templatesApi,
} from '../src/api/bindings'

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

describe('templatesApi', () => {
  it('lists templates and forwards archived flag', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(stubJsonResponse(200, []))
    await templatesApi.list()
    expect(fetch.mock.calls[0][0]).toBe('/api/templates')
    fetch.mockResolvedValueOnce(stubJsonResponse(200, []))
    await templatesApi.list({ includeArchived: true })
    expect(fetch.mock.calls[1][0]).toBe('/api/templates?include_archived=true')
  })

  it('creates a template with CSRF token', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(stubJsonResponse(201, { id: 't1' }))
    await templatesApi.create({ name: 'demo' }, 'csrf-t')
    const [path, init] = fetch.mock.calls[0]
    expect(path).toBe('/api/templates')
    expect(init.method).toBe('POST')
    expect(init.headers.get('X-CSRF-Token')).toBe('csrf-t')
    expect(JSON.parse(init.body)).toEqual({ name: 'demo' })
  })

  it('archives a template via expected_version', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(stubJsonResponse(200, { id: 't1' }))
    await templatesApi.archive('t1', 4, 'csrf-t')
    expect(fetch.mock.calls[0][0]).toBe(
      '/api/templates/t1/archive?expected_version=4',
    )
    expect(fetch.mock.calls[0][1].method).toBe('POST')
  })

  it('lists versions of a template', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(stubJsonResponse(200, []))
    await templatesApi.listVersions('t1')
    expect(fetch.mock.calls[0][0]).toBe('/api/templates/t1/versions')
  })

  it('creates a new version with PUT request payload', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(stubJsonResponse(201, { id: 'v1' }))
    await templatesApi.createVersion(
      't1',
      { graph: { nodes: [], edges: [] } },
      'csrf-t',
    )
    expect(fetch.mock.calls[0][0]).toBe('/api/templates/t1/versions')
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
      graph: { nodes: [], edges: [] },
    })
  })
})

describe('bindingsApi', () => {
  it('lists bindings with project_id query parameter', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(stubJsonResponse(200, []))
    await bindingsApi.list({ projectId: 'p1' })
    expect(fetch.mock.calls[0][0]).toBe('/api/bindings?project_id=p1')
  })

  it('loads resolved settings via GET endpoint', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(stubJsonResponse(200, { settings: [] }))
    await bindingsApi.resolved('b1')
    expect(fetch.mock.calls[0][0]).toBe('/api/bindings/b1/resolved')
    expect(fetch.mock.calls[0][1].method).toBeUndefined()
  })

  it('updates a binding with expected_version and CSRF', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(stubJsonResponse(200, { id: 'b1' }))
    await bindingsApi.update(
      'b1',
      {
        expected_version: 3,
        name: 'main',
        model_selections: {},
      },
      'csrf-b',
    )
    const [, init] = fetch.mock.calls[0]
    expect(JSON.parse(init.body)).toMatchObject({
      expected_version: 3,
      name: 'main',
    })
    expect(init.headers.get('X-CSRF-Token')).toBe('csrf-b')
  })

  it('archives a binding via expected_version', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(stubJsonResponse(200, { id: 'b1' }))
    await bindingsApi.archive('b1', 2, 'csrf-b')
    expect(fetch.mock.calls[0][0]).toBe(
      '/api/bindings/b1/archive?expected_version=2',
    )
  })
})

describe('presetsApi', () => {
  it('lists presets without parameters', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(stubJsonResponse(200, []))
    await presetsApi.list()
    expect(fetch.mock.calls[0][0]).toBe('/api/presets')
  })

  it('copies a preset and includes name when provided', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(stubJsonResponse(201, { id: 'tpl1' }))
    await presetsApi.copy('preset-a', 'csrf-p', 'My copy')
    const [path, init] = fetch.mock.calls[0]
    expect(path).toBe('/api/presets/preset-a/copy')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({ name: 'My copy' })
  })

  it('copies without name to produce an empty payload', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(stubJsonResponse(201, { id: 'tpl1' }))
    await presetsApi.copy('preset-a', 'csrf-p')
    const [, init] = fetch.mock.calls[0]
    expect(JSON.parse(init.body)).toEqual({})
  })
})

describe('describeBindingError from bindings module', () => {
  it('extracts api error message', () => {
    const error = new ApiError(409, {
      code: 'binding_conflict',
      message: 'Конфликт привязки',
      details: {},
      request_id: 'req',
      retryable: false,
    })
    expect(describeBindingError(error)).toBe('Конфликт привязки')
  })

  it('falls back to default for unknown errors', () => {
    expect(describeBindingError(null)).toBe('Неизвестная ошибка')
  })
})

describe('preflight catalog recovery', () => {
  const missing = {
    ok: false,
    execution_hash: null,
    warnings: [],
    errors: [
      {
        code: 'harness_catalog_unverified',
        message: 'Refresh',
        details: { harness_profile_id: 'h1' },
      },
      {
        code: 'harness_catalog_unverified',
        message: 'Refresh',
        details: { harness_profile_id: 'h1' },
      },
    ],
  }
  it('refreshes each affected harness once and repeats the same check', async () => {
    const fetch = captureFetch()
    fetch
      .mockResolvedValueOnce(stubJsonResponse(200, missing))
      .mockResolvedValueOnce(stubJsonResponse(200, { status: 'fresh' }))
      .mockResolvedValueOnce(
        stubJsonResponse(200, {
          ok: true,
          errors: [],
          warnings: [],
          execution_hash: 'new',
          candidates: [],
        }),
      )
    const report = await bindingsApi.preflight('b1', 'csrf', {
      inputs: { task: 'same' },
    })
    expect(report.ok).toBe(true)
    expect(report.execution_hash).toBe('new')
    expect(fetch.mock.calls.map((call) => call[0])).toEqual([
      '/api/bindings/b1/preflight',
      '/api/harness_profiles/h1/models/refresh?force=true',
      '/api/bindings/b1/preflight',
    ])
    expect(fetch.mock.calls[0][1].body).toBe(fetch.mock.calls[2][1].body)
    expect(fetch.mock.calls[1][1].headers.get('X-CSRF-Token')).toBe('csrf')
  })
  it('stops after one refresh when a model is still missing', async () => {
    const fetch = captureFetch()
    fetch
      .mockResolvedValueOnce(stubJsonResponse(200, missing))
      .mockResolvedValueOnce(stubJsonResponse(200, { status: 'fresh' }))
      .mockResolvedValueOnce(stubJsonResponse(200, missing))
    expect((await bindingsApi.preflight('b1', 'csrf')).ok).toBe(false)
    expect(fetch).toHaveBeenCalledTimes(3)
  })
  it('reports catalog failure without pretending preflight succeeded', async () => {
    const fetch = captureFetch()
    fetch
      .mockResolvedValueOnce(stubJsonResponse(200, missing))
      .mockResolvedValueOnce(
        stubJsonResponse(422, {
          code: 'harness_catalog_unavailable',
          message: 'Cannot load models',
          details: {},
        }),
      )
    await expect(bindingsApi.preflight('b1', 'csrf')).rejects.toThrow(
      'Cannot load models',
    )
    expect(fetch).toHaveBeenCalledTimes(2)
  })
})
