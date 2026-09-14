import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, request } from '../src/api/client'

afterEach(() => vi.unstubAllGlobals())

describe('local API client', () => {
  it('sends credentials and CSRF for a mutation without URL tokens', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetch)
    await request('/auth/logout', { method: 'POST' }, 'csrf-example')
    expect(fetch.mock.calls[0][0]).toBe('/api/auth/logout')
    expect(fetch.mock.calls[0][1].credentials).toBe('same-origin')
    expect(fetch.mock.calls[0][1].headers.get('X-CSRF-Token')).toBe(
      'csrf-example',
    )
  })
  it('preserves server error code and request ID', async () => {
    const body = {
      code: 'auth_required',
      message: 'Войдите',
      details: {},
      request_id: 'request-1',
      retryable: false,
    }
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(new Response(JSON.stringify(body), { status: 401 })),
    )
    try {
      await request('/auth/session')
      expect.fail('must reject unauthorized response')
    } catch (error) {
      expect(error).toBeInstanceOf(ApiError)
      expect((error as ApiError).body).toEqual(body)
    }
  })
})
