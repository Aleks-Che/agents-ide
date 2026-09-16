import { afterEach, describe, expect, it, vi } from 'vitest'
import { planningApi } from '../src/api/planning'
import { ApiError } from '../src/api/client'

afterEach(() => vi.unstubAllGlobals())
function capture(status = 200, value: unknown = {}) {
  const fetch = vi.fn().mockResolvedValue(
    new Response(JSON.stringify(value), {
      status,
      headers: { 'Content-Type': 'application/json' },
    }),
  )
  vi.stubGlobal('fetch', fetch)
  return fetch
}
describe('Council transport', () => {
  it('encodes filters and includes completed plans for history', async () => {
    const fetch = capture(200, [])
    expect(
      await planningApi.list({
        projectId: 'a b',
        chatId: 'c',
        includeCompleted: true,
      }),
    ).toEqual([])
    expect(fetch.mock.calls[0][0]).toBe(
      '/api/planning_jobs?project_id=a+b&chat_id=c&include_completed=true',
    )
  })
  it('preserves the creation idempotency key and group selection', async () => {
    const fetch = capture(201, { id: 'job' })
    const body = {
      project_id: 'p',
      task_text: 'Plan',
      idempotency_key: 'same-key',
      participants: [
        {
          role: 'participant' as const,
          selection: { kind: 'group' as const, group_id: 'g' },
        },
      ],
    }
    await planningApi.create(body, 'csrf-council')
    const [url, init] = fetch.mock.calls[0]
    expect(url).toBe('/api/planning_jobs')
    expect(JSON.parse(init.body)).toEqual(body)
    expect(init.headers.get('X-CSRF-Token')).toBe('csrf-council')
    expect(init.credentials).toBe('same-origin')
  })
  it('submits answers and full edited document for the displayed revision', async () => {
    const fetch = capture()
    const body = {
      expected_revision: 3,
      answers: [
        { question_id: 'q', kind: 'text' as const, free_text: 'Offline' },
      ],
      user_body_text: '{"questions":[]}',
    }
    await planningApi.submitAnswers('job', body, 'csrf')
    const [url, init] = fetch.mock.calls[0]
    expect(url).toBe('/api/planning_jobs/job/answers')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual(body)
    expect(init.headers.get('X-CSRF-Token')).toBe('csrf')
  })
  it('confirms the displayed hash without replacing it by a freshly fetched one', async () => {
    const fetch = capture()
    const body = { expected_revision: 2, confirmation_hash: 'displayed-hash' }
    await planningApi.confirm('job', body, 'csrf')
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(fetch.mock.calls[0][0]).toBe('/api/planning_jobs/job/confirm')
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual(body)
  })
  it('cancels with optimistic concurrency and propagates conflicts', async () => {
    const fetch = capture(409, {
      code: 'planning_state_version_invalid',
      message: 'Reload',
    })
    await expect(
      planningApi.cancel('job', { expected_state_version: 4 }, 'csrf'),
    ).rejects.toBeInstanceOf(ApiError)
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
      expected_state_version: 4,
    })
  })
  it('does not hide transport failure', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')))
    await expect(planningApi.get('job')).rejects.toThrow('offline')
  })
  it('retries only the displayed version with explicit access and unknown-outcome choices', async () => {
    const fetch = capture()
    const body = {
      expected_state_version: 7,
      reset_all_failed: true,
      refresh_credentials: true,
      acknowledge_unknown_result: false,
    }
    await planningApi.retry('job', body, 'csrf-retry')
    const [url, init] = fetch.mock.calls[0]
    expect(url).toBe('/api/planning_jobs/job/retry')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual(body)
    expect(init.headers.get('X-CSRF-Token')).toBe('csrf-retry')
  })
  it('sends an explicit subset of failed members when reset_all_failed is omitted', async () => {
    const fetch = capture()
    const body = {
      expected_state_version: 5,
      reset_all_failed: false,
      reset_member_indices: [0, 2],
      refresh_credentials: false,
      acknowledge_unknown_result: true,
    }
    await planningApi.retry('job', body, 'csrf-subset')
    const [url, init] = fetch.mock.calls[0]
    expect(url).toBe('/api/planning_jobs/job/retry')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual(body)
    expect(init.headers.get('X-CSRF-Token')).toBe('csrf-subset')
  })
  it('does not mix reset_all_failed with reset_member_indices on the wire', async () => {
    const fetch = capture()
    await planningApi.retry(
      'job',
      {
        expected_state_version: 1,
        reset_all_failed: false,
        reset_member_indices: [1],
      },
      'csrf-mix',
    )
    const body = JSON.parse(fetch.mock.calls[0][1].body)
    expect(body).not.toHaveProperty('reset_all_failed', true)
    expect(body.reset_member_indices).toEqual([1])
  })
  it('does not silently repeat a retry after a conflict', async () => {
    const fetch = capture(409, {
      code: 'planning_state_version_invalid',
      message: 'Reload',
    })
    await expect(
      planningApi.retry(
        'job',
        { expected_state_version: 1, reset_all_failed: true },
        'csrf',
      ),
    ).rejects.toBeInstanceOf(ApiError)
    expect(fetch).toHaveBeenCalledTimes(1)
  })
  it('surfaces a server-side rejection of an incomplete failed-members list', async () => {
    const fetch = capture(409, {
      code: 'planning_retry_unresolved',
      message: 'List all unsuccessful members',
    })
    await expect(
      planningApi.retry(
        'job',
        { expected_state_version: 3, reset_member_indices: [0] },
        'csrf',
      ),
    ).rejects.toBeInstanceOf(ApiError)
    expect(fetch).toHaveBeenCalledTimes(1)
  })
  it('promotes a single accepted draft only after explicit degraded confirmation', async () => {
    const fetch = capture()
    const body = {
      expected_state_version: 3,
      confirm_degraded: true,
    }
    await planningApi.promoteSingle('job', body, 'csrf')
    const [url, init] = fetch.mock.calls[0]
    expect(url).toBe('/api/planning_jobs/job/promote_single')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual(body)
    expect(init.headers.get('X-CSRF-Token')).toBe('csrf')
  })
  it('propagates conflicts when the single-member promotion races the state', async () => {
    const fetch = capture(409, {
      code: 'planning_state_version_invalid',
      message: 'Reload',
    })
    await expect(
      planningApi.promoteSingle(
        'job',
        { expected_state_version: 9, confirm_degraded: true },
        'csrf',
      ),
    ).rejects.toBeInstanceOf(ApiError)
    expect(fetch).toHaveBeenCalledTimes(1)
  })
})
