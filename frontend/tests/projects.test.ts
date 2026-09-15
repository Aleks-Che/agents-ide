import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../src/api/client'
import {
  chatsApi,
  describeError,
  messagesApi,
  projectsApi,
} from '../src/api/projects'
import { probeWorkspace, summariseProbe } from '../src/api/workspace'

afterEach(() => vi.unstubAllGlobals())

function stubFetchOnce(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('projectsApi', () => {
  it('lists projects without query string when archived not requested', async () => {
    const fetch = vi.fn().mockResolvedValue(stubFetchOnce(200, []))
    vi.stubGlobal('fetch', fetch)
    await projectsApi.list()
    expect(fetch.mock.calls[0][0]).toBe('/api/projects')
  })

  it('passes include_archived flag when requested', async () => {
    const fetch = vi.fn().mockResolvedValue(stubFetchOnce(200, []))
    vi.stubGlobal('fetch', fetch)
    await projectsApi.list({ includeArchived: true })
    expect(fetch.mock.calls[0][0]).toBe('/api/projects?include_archived=true')
  })

  it('creates a project with CSRF token and JSON body', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(
        stubFetchOnce(201, { id: 'p1', name: 'Demo', version: 1 }),
      )
    vi.stubGlobal('fetch', fetch)
    await projectsApi.create(
      { name: 'Demo', workspace_path: 'C:/work/demo' },
      'csrf-1',
    )
    const [path, init] = fetch.mock.calls[0]
    expect(path).toBe('/api/projects')
    expect(init.method).toBe('POST')
    expect(init.headers.get('X-CSRF-Token')).toBe('csrf-1')
    expect(JSON.parse(init.body)).toEqual({
      name: 'Demo',
      workspace_path: 'C:/work/demo',
    })
  })
})

describe('chatsApi and messagesApi', () => {
  it('lists chats with project id path', async () => {
    const fetch = vi.fn().mockResolvedValue(stubFetchOnce(200, []))
    vi.stubGlobal('fetch', fetch)
    await chatsApi.list({ projectId: 'p1' })
    expect(fetch.mock.calls[0][0]).toBe('/api/projects/p1/chats')
  })

  it('sends archive command with expected version', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(stubFetchOnce(200, { id: 'c1', archived: true }))
    vi.stubGlobal('fetch', fetch)
    await chatsApi.archive(
      'c1',
      { archive: true, expected_version: 4 },
      'csrf-2',
    )
    const [, init] = fetch.mock.calls[0]
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({
      archive: true,
      expected_version: 4,
    })
  })

  it('lists messages with optional limit', async () => {
    const fetch = vi.fn().mockResolvedValue(stubFetchOnce(200, []))
    vi.stubGlobal('fetch', fetch)
    await messagesApi.list({ chatId: 'c1', limit: 50 })
    expect(fetch.mock.calls[0][0]).toBe('/api/chats/c1/messages?limit=50')
  })
})

describe('workspace probe', () => {
  it('serialises the path and posts to /workspace/probe', async () => {
    const fetch = vi.fn().mockResolvedValue(
      stubFetchOnce(200, {
        entered_path: 'C:/work/d',
        normalized_path: 'C:/work/d',
        git: { root_path: null },
      }),
    )
    vi.stubGlobal('fetch', fetch)
    await probeWorkspace('C:/work/d', 'csrf-probe')
    const [path, init] = fetch.mock.calls[0]
    expect(path).toBe('/api/workspace/probe')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({ path: 'C:/work/d' })
    expect(init.headers.get('X-CSRF-Token')).toBe('csrf-probe')
  })

  it('summarises workspace probe response', () => {
    const summary = summariseProbe({
      entered_path: 'C:/work/d',
      normalized_path: 'C:\\work\\d',
      git: {
        root_path: 'C:/work/d/.git',
        default_branch: 'main',
        dirty: true,
        head_sha: '1234567890',
      },
    })
    expect(summary.isGitRepo).toBe(true)
    expect(summary.gitDefaultBranch).toBe('main')
    expect(summary.gitDirty).toBe(true)
    expect(summary.normalizedPath).toBe('C:\\work\\d')
  })
})

describe('describeError', () => {
  it('extracts message from ApiError body', () => {
    const error = new ApiError(409, {
      code: 'version_conflict',
      message: 'Конфликт версии',
      details: {},
      request_id: 'req-1',
      retryable: false,
    })
    expect(describeError(error)).toBe('Конфликт версии')
  })

  it('falls back to Error message and default for unknown input', () => {
    expect(describeError(new Error('boom'))).toBe('boom')
    expect(describeError('plain')).toBe('Неизвестная ошибка')
    expect(describeError(null)).toBe('Неизвестная ошибка')
  })
})
