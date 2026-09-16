import { ApiError, request } from './client'
import type { ApiSchemas } from './generated'

export type HarnessProfile = ApiSchemas['HarnessProfile']
export type HarnessProfileCatalog = ApiSchemas['HarnessProfileCatalog']
export type HarnessProbe = ApiSchemas['HarnessProbe']

export interface HarnessQuery {
  includeArchived?: boolean
}

export const harnessApi = {
  list(query: HarnessQuery = {}): Promise<HarnessProfile[]> {
    const params = new URLSearchParams()
    if (query.includeArchived) params.set('include_archived', 'true')
    const search = params.toString()
    return request<HarnessProfile[]>(
      `/harness_profiles${search ? `?${search}` : ''}`,
    )
  },
  get(id: string): Promise<HarnessProfile> {
    return request<HarnessProfile>(`/harness_profiles/${id}`)
  },
  create(
    body: ApiSchemas['HarnessProfileCreate'],
    csrf: string,
  ): Promise<HarnessProfile> {
    return request<HarnessProfile>(
      '/harness_profiles',
      { method: 'POST', body: JSON.stringify(body) },
      csrf,
    )
  },
  update(
    id: string,
    body: ApiSchemas['HarnessProfileUpdate'],
    csrf: string,
  ): Promise<HarnessProfile> {
    return request<HarnessProfile>(
      `/harness_profiles/${id}`,
      { method: 'PATCH', body: JSON.stringify(body) },
      csrf,
    )
  },
  archive(
    id: string,
    expectedVersion: number,
    csrf: string,
  ): Promise<HarnessProfile> {
    return request<HarnessProfile>(
      `/harness_profiles/${id}/archive?expected_version=${expectedVersion}`,
      { method: 'POST' },
      csrf,
    )
  },
  catalog(id: string): Promise<HarnessProfileCatalog> {
    return request<HarnessProfileCatalog>(`/harness_profiles/${id}/models`)
  },
  probe(id: string, csrf: string): Promise<HarnessProbe> {
    return request<HarnessProbe>(
      `/harness_profiles/${id}/test`,
      { method: 'POST' },
      csrf,
    )
  },
}

export type ProviderConnection = ApiSchemas['ProviderConnection']
export type ProviderTest = ApiSchemas['ProviderTest']
export type ConnectionModels = Record<string, unknown>

export interface ConnectionQuery {
  includeArchived?: boolean
}

export const connectionsApi = {
  list(query: ConnectionQuery = {}): Promise<ProviderConnection[]> {
    const params = new URLSearchParams()
    if (query.includeArchived) params.set('include_archived', 'true')
    const search = params.toString()
    return request<ProviderConnection[]>(
      `/connections${search ? `?${search}` : ''}`,
    )
  },
  get(id: string): Promise<ProviderConnection> {
    return request<ProviderConnection>(`/connections/${id}`)
  },
  create(
    body: ApiSchemas['ProviderConnectionCreate'],
    csrf: string,
  ): Promise<ProviderConnection> {
    return request<ProviderConnection>(
      '/connections',
      { method: 'POST', body: JSON.stringify(body) },
      csrf,
    )
  },
  update(
    id: string,
    body: ApiSchemas['ProviderConnectionUpdate'],
    csrf: string,
  ): Promise<ProviderConnection> {
    return request<ProviderConnection>(
      `/connections/${id}`,
      { method: 'PATCH', body: JSON.stringify(body) },
      csrf,
    )
  },
  archive(
    id: string,
    expectedVersion: number,
    csrf: string,
  ): Promise<ProviderConnection> {
    return request<ProviderConnection>(
      `/connections/${id}/archive?expected_version=${expectedVersion}`,
      { method: 'POST' },
      csrf,
    )
  },
  test(id: string, csrf: string): Promise<ProviderTest> {
    return request<ProviderTest>(
      `/connections/${id}/test`,
      { method: 'POST' },
      csrf,
    )
  },
  models(id: string): Promise<ConnectionModels> {
    return request<ConnectionModels>(`/connections/${id}/models`)
  },
}

export type ModelGroup = ApiSchemas['ModelGroup']
export type ModelGroupExport = ApiSchemas['ModelGroupExport']

export type ModelGroupMember = ModelGroup['members'][number]

export interface ModelGroupQuery {
  kind?: 'agent' | 'llm'
  includeArchived?: boolean
}

export const groupsApi = {
  export(id: string): Promise<ModelGroupExport> {
    return request<ModelGroupExport>(`/model_groups/${id}/export`)
  },
  import(
    body: ApiSchemas['ModelGroupImport'],
    csrf: string,
  ): Promise<ModelGroup> {
    return request<ModelGroup>(
      '/model_groups/import',
      { method: 'POST', body: JSON.stringify(body) },
      csrf,
    )
  },
  list(query: ModelGroupQuery = {}): Promise<ModelGroup[]> {
    const params = new URLSearchParams()
    if (query.kind) params.set('kind', query.kind)
    if (query.includeArchived) params.set('include_archived', 'true')
    const search = params.toString()
    return request<ModelGroup[]>(`/model_groups${search ? `?${search}` : ''}`)
  },
  get(id: string): Promise<ModelGroup> {
    return request<ModelGroup>(`/model_groups/${id}`)
  },
  createAgent(
    body: ApiSchemas['ModelGroupAgentCreate'],
    csrf: string,
  ): Promise<ModelGroup> {
    return request<ModelGroup>(
      '/model_groups/agent',
      { method: 'POST', body: JSON.stringify(body) },
      csrf,
    )
  },
  createLLM(
    body: ApiSchemas['ModelGroupLLMCreate'],
    csrf: string,
  ): Promise<ModelGroup> {
    return request<ModelGroup>(
      '/model_groups/llm',
      { method: 'POST', body: JSON.stringify(body) },
      csrf,
    )
  },
  updateAgent(
    id: string,
    body: ApiSchemas['ModelGroupAgentUpdate'],
    csrf: string,
  ): Promise<ModelGroup> {
    return request<ModelGroup>(
      `/model_groups/${id}/agent`,
      { method: 'PATCH', body: JSON.stringify(body) },
      csrf,
    )
  },
  updateLLM(
    id: string,
    body: ApiSchemas['ModelGroupLLMUpdate'],
    csrf: string,
  ): Promise<ModelGroup> {
    return request<ModelGroup>(
      `/model_groups/${id}/llm`,
      { method: 'PATCH', body: JSON.stringify(body) },
      csrf,
    )
  },
  replaceAgentMembers(
    id: string,
    body: ApiSchemas['ModelGroupAgentMembersReplace'],
    csrf: string,
  ): Promise<ModelGroup> {
    return request<ModelGroup>(
      `/model_groups/${id}/agent/members`,
      { method: 'PUT', body: JSON.stringify(body) },
      csrf,
    )
  },
  replaceLLMMembers(
    id: string,
    body: ApiSchemas['ModelGroupLLMMembersReplace'],
    csrf: string,
  ): Promise<ModelGroup> {
    return request<ModelGroup>(
      `/model_groups/${id}/llm/members`,
      { method: 'PUT', body: JSON.stringify(body) },
      csrf,
    )
  },
  deleteMember(
    groupId: string,
    memberId: string,
    expectedRevision: number,
    csrf: string,
  ): Promise<ModelGroup> {
    return request<ModelGroup>(
      `/model_groups/${groupId}/members/${memberId}?expected_revision=${expectedRevision}`,
      { method: 'DELETE' },
      csrf,
    )
  },
  archive(
    id: string,
    expectedRevision: number,
    csrf: string,
  ): Promise<ModelGroup> {
    return request<ModelGroup>(
      `/model_groups/${id}/archive?expected_revision=${expectedRevision}`,
      { method: 'POST' },
      csrf,
    )
  },
  copy(
    id: string,
    body: ApiSchemas['ModelGroupCopy'],
    csrf: string,
  ): Promise<ModelGroup> {
    return request<ModelGroup>(
      `/model_groups/${id}/copy`,
      { method: 'POST', body: JSON.stringify(body) },
      csrf,
    )
  },
}

export function describeError(error: unknown): string {
  if (error instanceof ApiError) return error.body.message
  if (error instanceof Error) return error.message
  return 'Неизвестная ошибка'
}
