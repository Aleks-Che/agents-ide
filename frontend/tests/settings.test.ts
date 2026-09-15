import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../src/api/client'
import {
  connectionsApi,
  describeError,
  groupsApi,
  harnessApi,
} from '../src/api/settings'

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

describe('harnessApi', () => {
  it('lists profiles and forwards archived flag', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(stubJsonResponse(200, []))
    await harnessApi.list()
    expect(fetch.mock.calls[0][0]).toBe('/api/harness_profiles')
    fetch.mockResolvedValueOnce(stubJsonResponse(200, []))
    await harnessApi.list({ includeArchived: true })
    expect(fetch.mock.calls[1][0]).toBe(
      '/api/harness_profiles?include_archived=true',
    )
  })

  it('creates a profile with CSRF token', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(
      stubJsonResponse(201, { id: 'p1', name: 'OpenCode local' }),
    )
    await harnessApi.create(
      { name: 'OpenCode local', harness_kind: 'opencode' },
      'csrf-h',
    )
    const [path, init] = fetch.mock.calls[0]
    expect(path).toBe('/api/harness_profiles')
    expect(init.method).toBe('POST')
    expect(init.headers.get('X-CSRF-Token')).toBe('csrf-h')
    expect(JSON.parse(init.body)).toEqual({
      name: 'OpenCode local',
      harness_kind: 'opencode',
    })
  })

  it('archives a profile using expected_version query parameter', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(stubJsonResponse(200, { id: 'p1' }))
    await harnessApi.archive('p1', 3, 'csrf-h')
    const [path, init] = fetch.mock.calls[0]
    expect(path).toBe('/api/harness_profiles/p1/archive?expected_version=3')
    expect(init.method).toBe('POST')
    expect(init.headers.get('X-CSRF-Token')).toBe('csrf-h')
  })

  it('probes a profile', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(
      stubJsonResponse(200, { status: 'ok', version: '1.0.0', detail: null }),
    )
    await harnessApi.probe('p1', 'csrf-h')
    const [path, init] = fetch.mock.calls[0]
    expect(path).toBe('/api/harness_profiles/p1/test')
    expect(init.method).toBe('POST')
  })
})

describe('connectionsApi', () => {
  it('archives with the required configuration version and CSRF', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(
      stubJsonResponse(200, { id: 'c1', archived: true }),
    )
    await connectionsApi.archive('c1', 4, 'csrf-c')
    expect(fetch.mock.calls[0][0]).toBe(
      '/api/connections/c1/archive?expected_version=4',
    )
    expect(fetch.mock.calls[0][1].headers.get('X-CSRF-Token')).toBe('csrf-c')
  })
  it('creates a connection without secret and serialises manual_models', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(stubJsonResponse(201, { id: 'c1' }))
    await connectionsApi.create(
      {
        name: 'Local OpenAI',
        base_url: 'http://127.0.0.1:8080/v1',
        manual_models: ['custom-model'],
      },
      'csrf-c',
    )
    const [, init] = fetch.mock.calls[0]
    expect(JSON.parse(init.body)).toEqual({
      name: 'Local OpenAI',
      base_url: 'http://127.0.0.1:8080/v1',
      manual_models: ['custom-model'],
    })
  })

  it('updates connection with masked secret leaving manual_models untouched', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(stubJsonResponse(200, { id: 'c1' }))
    await connectionsApi.update(
      'c1',
      {
        expected_version: 2,
        name: 'Local OpenAI',
        base_url: 'http://127.0.0.1:8080/v1',
        secret: null,
        manual_models: ['custom-model'],
      },
      'csrf-c',
    )
    const [, init] = fetch.mock.calls[0]
    expect(JSON.parse(init.body)).toMatchObject({
      expected_version: 2,
      manual_models: ['custom-model'],
    })
  })

  it('runs test probe with CSRF', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(
      stubJsonResponse(200, {
        status: 'ok',
        detail: null,
        tested_at: '2026-09-15T00:00:00Z',
        models: [],
      }),
    )
    await connectionsApi.test('c1', 'csrf-c')
    const [path, init] = fetch.mock.calls[0]
    expect(path).toBe('/api/connections/c1/test')
    expect(init.method).toBe('POST')
    expect(init.headers.get('X-CSRF-Token')).toBe('csrf-c')
  })
})

describe('groupsApi', () => {
  it('filters groups by kind', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(stubJsonResponse(200, []))
    await groupsApi.list({ kind: 'agent' })
    expect(fetch.mock.calls[0][0]).toBe('/api/model_groups?kind=agent')
  })

  it('creates an agent group with single member', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(stubJsonResponse(201, { id: 'g1' }))
    await groupsApi.createAgent(
      {
        name: 'heavy',
        members: [
          {
            harness_profile_id: 'h1',
            model_id: 'm1',
            enabled: true,
          },
        ],
      },
      'csrf-g',
    )
    const [path, init] = fetch.mock.calls[0]
    expect(path).toBe('/api/model_groups/agent')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toMatchObject({
      name: 'heavy',
      members: [{ harness_profile_id: 'h1', model_id: 'm1', enabled: true }],
    })
  })

  it('replaces LLM group members atomically', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(stubJsonResponse(200, { id: 'g1' }))
    await groupsApi.replaceLLMMembers(
      'g1',
      {
        expected_revision: 4,
        members: [
          {
            id: 'm1',
            provider_connection_id: 'c1',
            model_id: 'gpt-4.1',
            enabled: true,
          },
        ],
      },
      'csrf-g',
    )
    const [path, init] = fetch.mock.calls[0]
    expect(path).toBe('/api/model_groups/g1/llm/members')
    expect(init.method).toBe('PUT')
    expect(JSON.parse(init.body)).toEqual({
      expected_revision: 4,
      members: [
        {
          id: 'm1',
          provider_connection_id: 'c1',
          model_id: 'gpt-4.1',
          enabled: true,
        },
      ],
    })
  })

  it('archives a group using expected_revision query', async () => {
    const fetch = captureFetch()
    fetch.mockResolvedValueOnce(stubJsonResponse(200, { id: 'g1' }))
    await groupsApi.archive('g1', 5, 'csrf-g')
    expect(fetch.mock.calls[0][0]).toBe(
      '/api/model_groups/g1/archive?expected_revision=5',
    )
  })
})

describe('describeError from settings module', () => {
  it('extracts api error message', () => {
    const error = new ApiError(422, {
      code: 'invalid',
      message: 'Неверные параметры',
      details: {},
      request_id: 'req',
      retryable: false,
    })
    expect(describeError(error)).toBe('Неверные параметры')
  })

  it('falls back to default message for unknown error', () => {
    expect(describeError(null)).toBe('Неизвестная ошибка')
  })
})
