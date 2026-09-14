export interface ApiErrorBody {
  code: string
  message: string
  details: Record<string, unknown>
  request_id: string
  retryable: boolean
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public body: ApiErrorBody,
  ) {
    super(body.message)
  }
}

export interface Session {
  csrf_token: string
  expires_at: number
}

export interface SystemStatus {
  api: string
  database: string
  worker: { status: string; last_seen_at: number | null }
  version: string
  ready: boolean
}

export async function request<T>(
  path: string,
  options: RequestInit = {},
  csrf?: string,
): Promise<T> {
  const headers = new Headers(options.headers)
  if (options.body) headers.set('Content-Type', 'application/json')
  if (csrf) headers.set('X-CSRF-Token', csrf)
  const response = await fetch(`/api${path}`, {
    ...options,
    headers,
    credentials: 'same-origin',
  })
  if (!response.ok) {
    let body: ApiErrorBody
    try {
      body = await response.json()
    } catch {
      body = {
        code: 'connection_error',
        message: 'API недоступен. Проверьте запуск службы.',
        details: {},
        request_id: response.headers.get('X-Request-ID') ?? '',
        retryable: true,
      }
    }
    throw new ApiError(response.status, body)
  }
  return response.status === 204 ? (undefined as T) : response.json()
}
