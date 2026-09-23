import { ApiError, request } from './client'
import type { ApiSchemas } from './generated'

export type RunRecord = ApiSchemas['Run']
export type GitChangesReview = ApiSchemas['GitChangesReview']
export type RunCommand = ApiSchemas['RunCommand']
export type CommandAccepted = ApiSchemas['CommandAccepted']
export type EventEnvelope = ApiSchemas['EventEnvelope']
export type EventBatchResponse = ApiSchemas['EventBatchResponse']
export type ArtifactView = ApiSchemas['ArtifactView']
export type RunSnapshot = ApiSchemas['RunSnapshot']
export type RunObservation = ApiSchemas['RunObservation']
export type HistoryPage = ApiSchemas['HistoryPage']
export type HistoryCategory =
  'all' | 'steps' | 'messages' | 'tools' | 'models' | 'commands' | 'checks'
export type WaitingReason = ApiSchemas['WaitingReason']
export type SelectionSummary = NonNullable<
  ApiSchemas['RunSnapshot']['selection']
>
export type SelectionNodeEntry = NonNullable<SelectionSummary['nodes']>[number]
export type SelectionCandidateEntry = NonNullable<
  SelectionNodeEntry['candidates']
>[number]
export type SelectionGroupRef = NonNullable<SelectionSummary['groups']>[number]
export type SelectionVisit = NonNullable<SelectionSummary['visit']>

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

export type CandidateState = SelectionCandidateEntry['state']

export const CANDIDATE_STATE_LABELS: Record<string, string> = {
  available: 'Ожидает выбора',
  skipped: 'Пропущен',
  consumed: 'Использован',
  current: 'Текущий',
  selected: 'Выбран',
  succeeded: 'Выполнен',
}

export const MODEL_GROUP_EVENT_TYPES: ReadonlySet<string> = new Set([
  'model_group.candidate_selected',
  'model_group.candidate_skipped',
  'model_group.candidate_switched',
  'model_group.exhausted',
])

export const runsApi = {
  gitChanges(runId: string): Promise<GitChangesReview> {
    return request(`/runs/${runId}/git-changes`)
  },
  artifactContent(
    runId: string,
    artifactId: string,
    offset: number,
    format: 'text' | 'json',
  ): Promise<ApiSchemas['ArtifactContent']> {
    return request(
      `/runs/${runId}/artifacts/${artifactId}/content?offset=${offset}&limit=16000&format=${format}`,
    )
  },
  history(
    runId: string,
    query: {
      before?: number
      category?: HistoryCategory
      nodeId?: string
      executionId?: string
    } = {},
  ): Promise<HistoryPage> {
    const params = new URLSearchParams({ limit: '200' })
    if (query.before !== undefined) params.set('before', String(query.before))
    if (query.category) params.set('category', query.category)
    if (query.nodeId) params.set('node_id', query.nodeId)
    if (query.executionId) params.set('execution_id', query.executionId)
    return request(`/runs/${runId}/history?${params}`)
  },
  replay(runId: string): Promise<EventBatchResponse> {
    return request<EventBatchResponse>(`/runs/${runId}/events/replay`)
  },
  list(
    query: { projectId?: string; chatId?: string } = {},
  ): Promise<RunRecord[]> {
    const params = new URLSearchParams()
    if (query.projectId) params.set('project_id', query.projectId)
    if (query.chatId) params.set('chat_id', query.chatId)
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
  restart(
    runId: string,
    body: ApiSchemas['RunRestart'],
    csrf: string,
  ): Promise<RunRecord> {
    return request<RunRecord>(
      `/runs/${runId}/restart`,
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
  artifacts(runId: string, offset = 0): Promise<ArtifactView[]> {
    return request<ArtifactView[]>(
      `/runs/${runId}/artifacts?offset=${offset}&limit=50`,
    )
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
  commandJournal(runId: string, offset = 0): Promise<CommandAccepted[]> {
    return request<CommandAccepted[]>(
      `/runs/${runId}/commands?offset=${offset}&limit=50`,
    )
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
