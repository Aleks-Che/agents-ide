import { request } from './client'
import type { ApiSchemas } from './generated'

export type AssistanceTarget = ApiSchemas['AssistanceTarget']
export type AssistanceContext = ApiSchemas['AssistanceContext']
export type AssistanceGuide = ApiSchemas['AssistanceGuide']
export type AssistanceModel = ApiSchemas['AssistanceModel']
export type AssistanceTurn = ApiSchemas['AssistanceTurn']
export type AssistanceTool = ApiSchemas['AssistanceTool']
export type AssistanceToolResult = ApiSchemas['AssistanceToolResult']
export type CommitConnectionReview = ApiSchemas['CommitConnectionReview']

export const assistanceApi = {
  reviewCommitConnection: (runId: string) =>
    request<CommitConnectionReview>(
      `/assistance/runs/${runId}/commit-connection`,
    ),
  executeTool: (payload: ApiSchemas['AssistanceToolRequest'], csrf: string) =>
    request<AssistanceToolResult>(
      '/assistance/tools/execute',
      {
        method: 'POST',
        body: JSON.stringify(payload),
      },
      csrf,
    ),
  catalog: () => request<AssistanceGuide[]>('/assistance/catalog'),
  models: () => request<AssistanceModel[]>('/assistance/models'),
  context: (target: AssistanceTarget) => {
    const query = new URLSearchParams({ zone: target.zone })
    if (target.project_id) query.set('project_id', target.project_id)
    if (target.chat_id) query.set('chat_id', target.chat_id)
    return request<AssistanceContext>(`/assistance/context?${query}`)
  },
  suggestions: (
    payload: ApiSchemas['SuggestionsRequest'],
    csrf: string,
    signal: AbortSignal,
  ) =>
    request<ApiSchemas['AssistanceSuggestions']>(
      '/assistance/suggestions',
      {
        method: 'POST',
        body: JSON.stringify(payload),
        signal,
      },
      csrf,
    ),
  send: (
    payload: ApiSchemas['AssistanceMessage'],
    csrf: string,
    signal: AbortSignal,
  ) =>
    request<ApiSchemas['AssistanceAnswer']>(
      '/assistance/messages',
      {
        method: 'POST',
        body: JSON.stringify(payload),
        signal,
      },
      csrf,
    ),
}
