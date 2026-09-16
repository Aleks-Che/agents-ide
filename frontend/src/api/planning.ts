import { request } from './client'
import type { ApiSchemas } from './generated'

export type PlanningMemberSpec = ApiSchemas['PlanningMemberSpec']
export type PlanningAnswerInput = ApiSchemas['PlanningAnswerInput']
export type PlanningJobView = ApiSchemas['PlanningJobView']
export type PlanningQuestion = ApiSchemas['PlanningQuestion']
export type PlanningRevisionView = ApiSchemas['PlanningRevisionView']
export type PlanningAnswersAccepted = ApiSchemas['PlanningAnswersAccepted']
export type PlanningConfirmed = ApiSchemas['PlanningConfirmed']
export type PlanningRetryRequest = ApiSchemas['PlanningRetryRequest']
export type PlanningRetried = ApiSchemas['PlanningRetried']
export type PlanningPromoteSingleRequest =
  ApiSchemas['PlanningPromoteSingleRequest']
export type PlanningPromoteSingle = ApiSchemas['PlanningPromoteSingle']
export type PlanningJobCreatePayload = ApiSchemas['PlanningJobCreate']
export type PlanningConfirmationHash = {
  job_id: string
  revision_number: number
  questions_hash: string
  confirmation_hash: string
  readiness: PlanningRevisionView['readiness']
}
export type PlanningModelSelection = PlanningMemberSpec['selection']
export type PlanningSource = ApiSchemas['PlanningSource']

export interface PlanningQuery {
  projectId?: string
  chatId?: string
  includeCompleted?: boolean
}

export const planningApi = {
  list(query: PlanningQuery = {}): Promise<PlanningJobView[]> {
    const params = new URLSearchParams()
    if (query.projectId) params.set('project_id', query.projectId)
    if (query.chatId) params.set('chat_id', query.chatId)
    if (query.includeCompleted) params.set('include_completed', 'true')
    const search = params.toString()
    return request<PlanningJobView[]>(
      `/planning_jobs${search ? `?${search}` : ''}`,
    )
  },
  get(jobId: string): Promise<PlanningJobView> {
    return request<PlanningJobView>(`/planning_jobs/${jobId}`)
  },
  create(
    body: PlanningJobCreatePayload,
    csrf: string,
  ): Promise<PlanningJobView> {
    return request<PlanningJobView>(
      '/planning_jobs',
      { method: 'POST', body: JSON.stringify(body) },
      csrf,
    )
  },
  cancel(
    jobId: string,
    body: { expected_state_version: number; reason?: string },
    csrf: string,
  ): Promise<PlanningJobView> {
    return request<PlanningJobView>(
      `/planning_jobs/${jobId}/cancel`,
      { method: 'POST', body: JSON.stringify(body) },
      csrf,
    )
  },
  retry(
    jobId: string,
    body: PlanningRetryRequest,
    csrf: string,
  ): Promise<PlanningRetried> {
    return request<PlanningRetried>(
      `/planning_jobs/${jobId}/retry`,
      { method: 'POST', body: JSON.stringify(body) },
      csrf,
    )
  },
  promoteSingle(
    jobId: string,
    body: PlanningPromoteSingleRequest,
    csrf: string,
  ): Promise<PlanningPromoteSingle> {
    return request<PlanningPromoteSingle>(
      `/planning_jobs/${jobId}/promote_single`,
      { method: 'POST', body: JSON.stringify(body) },
      csrf,
    )
  },
  submitAnswers(
    jobId: string,
    body: {
      expected_revision: number
      answers: PlanningAnswerInput[]
      user_body_text?: string | null
    },
    csrf: string,
  ): Promise<PlanningAnswersAccepted> {
    return request<PlanningAnswersAccepted>(
      `/planning_jobs/${jobId}/answers`,
      { method: 'POST', body: JSON.stringify(body) },
      csrf,
    )
  },
  confirm(
    jobId: string,
    body: { expected_revision: number; confirmation_hash: string },
    csrf: string,
  ): Promise<PlanningConfirmed> {
    return request<PlanningConfirmed>(
      `/planning_jobs/${jobId}/confirm`,
      { method: 'POST', body: JSON.stringify(body) },
      csrf,
    )
  },
  confirmationHash(
    jobId: string,
    revisionNumber: number,
  ): Promise<PlanningConfirmationHash> {
    return request<PlanningConfirmationHash>(
      `/planning_jobs/${jobId}/hash?revision_number=${revisionNumber}`,
    )
  },
}
