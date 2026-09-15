import { ApiError, request } from './client'
import type { ApiSchemas } from './generated'

export type RunRecord = ApiSchemas['Run']
export type RunCommand = ApiSchemas['RunCommand']
export type CommandAccepted = ApiSchemas['CommandAccepted']
export type EventEnvelope = ApiSchemas['EventEnvelope']
export type EventBatchResponse = ApiSchemas['EventBatchResponse']
export type ArtifactView = ApiSchemas['ArtifactView']
export type RunSnapshot = ApiSchemas['RunSnapshot']
export type WaitingReason = ApiSchemas['WaitingReason']

export interface RunPlanResponse {
  plan: Record<string, unknown> | null
  summary: Record<string, unknown>
  items: Array<{
    id: string
    order: number
    title: string
    acceptance_criteria: string[]
    status: 'pending' | 'in_progress' | 'done' | 'failed'
    evidence_ids: string[]
    commit_shas: string[]
    related_execution_ids: string[]
  }>
}

export interface RunDiagnostics {
  run_id: string
  state: string
  state_version: number
  resume_target: Record<string, unknown>
  stop_goal: string | null
  waiting_reason: WaitingReason | null
  attempt: {
    id: string
    status: string
    heartbeat_at: number | null
    operation_id: string | null
  } | null
  processes: Array<{
    id: string
    pid: number | null
    create_time: number | null
    owner_generation: number
    state: string
    health: string | null
    last_health_ok: boolean | null
    last_external_event_at: number | null
    transport: string | null
    port: number | null
  }>
  reservation_ids: string[]
}

export const runsApi = {
  replay(runId: string): Promise<EventBatchResponse> {
    return request<EventBatchResponse>(`/runs/${runId}/events/replay`)
  },
  list(projectId?: string): Promise<RunRecord[]> {
    const params = new URLSearchParams()
    if (projectId) params.set('project_id', projectId)
    const search = params.toString()
    return request<RunRecord[]>(`/runs${search ? `?${search}` : ''}`)
  },
  get(runId: string): Promise<RunRecord> {
    return request<RunRecord>(`/runs/${runId}`)
  },
  start(body: ApiSchemas['RunStart'], csrf: string): Promise<RunRecord> {
    return request<RunRecord>(
      '/runs',
      { method: 'POST', body: JSON.stringify(body) },
      csrf,
    )
  },
  snapshot(runId: string): Promise<RunSnapshot> {
    return request<RunSnapshot>(`/runs/${runId}/snapshot`)
  },
  plan(runId: string): Promise<RunPlanResponse> {
    return request<RunPlanResponse>(`/runs/${runId}/plan`)
  },
  diagnostics(runId: string): Promise<RunDiagnostics> {
    return request<RunDiagnostics>(`/runs/${runId}/diagnostics`)
  },
  artifacts(runId: string): Promise<ArtifactView[]> {
    return request<ArtifactView[]>(`/runs/${runId}/artifacts`)
  },
  artifact(runId: string, artifactId: string): Promise<ArtifactView> {
    return request<ArtifactView>(`/runs/${runId}/artifacts/${artifactId}`)
  },
  events(
    runId: string,
    query: { after?: number; limit?: number } = {},
  ): Promise<EventBatchResponse> {
    const params = new URLSearchParams()
    if (query.after !== undefined) params.set('after', String(query.after))
    if (query.limit) params.set('limit', String(query.limit))
    const search = params.toString()
    return request<EventBatchResponse>(
      `/runs/${runId}/events${search ? `?${search}` : ''}`,
    )
  },
  commandJournal(runId: string): Promise<CommandAccepted[]> {
    return request<CommandAccepted[]>(`/runs/${runId}/commands`)
  },
  submitCommand(
    runId: string,
    body: RunCommand,
    csrf: string,
  ): Promise<CommandAccepted> {
    return request<CommandAccepted>(
      `/runs/${runId}/commands`,
      { method: 'POST', body: JSON.stringify(body) },
      csrf,
    )
  },
}

export function describeRunError(error: unknown): string {
  if (error instanceof ApiError) return error.body.message
  if (error instanceof Error) return error.message
  return 'Неизвестная ошибка'
}
