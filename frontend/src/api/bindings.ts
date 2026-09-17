import { ApiError, request } from './client'
import type { ApiSchemas } from './generated'

export type PipelineTemplate = ApiSchemas['PipelineTemplate']
export type PipelineVersion = ApiSchemas['PipelineVersion']
export type PipelineBinding = ApiSchemas['PipelineBinding']
export type ResolvedSettings = ApiSchemas['ResolvedSettings']
export type PresetSummary = {
  id: string
  name: string
  description: string
  template_id: string
  preset_version: string
  roles: Record<string, 'agent' | 'llm'>
}

export interface TemplateQuery {
  includeArchived?: boolean
}

export interface BindingQuery {
  projectId?: string
  includeArchived?: boolean
}

export interface PreflightIssue {
  code: string
  message: string
  node_id?: string
  details?: Record<string, unknown>
}

export interface PreflightReport {
  ok: boolean
  execution_hash: string | null
  errors: PreflightIssue[]
  warnings: PreflightIssue[]
  preview: Record<string, unknown>
}

export const templatesApi = {
  list(query: TemplateQuery = {}): Promise<PipelineTemplate[]> {
    const params = new URLSearchParams()
    if (query.includeArchived) params.set('include_archived', 'true')
    const search = params.toString()
    return request<PipelineTemplate[]>(
      `/templates${search ? `?${search}` : ''}`,
    )
  },
  get(templateId: string): Promise<PipelineTemplate> {
    return request<PipelineTemplate>(`/templates/${templateId}`)
  },
  create(
    body: ApiSchemas['PipelineTemplateCreate'],
    csrf: string,
  ): Promise<PipelineTemplate> {
    return request<PipelineTemplate>(
      '/templates',
      { method: 'POST', body: JSON.stringify(body) },
      csrf,
    )
  },
  update(
    templateId: string,
    body: ApiSchemas['PipelineTemplateUpdate'],
    csrf: string,
  ): Promise<PipelineTemplate> {
    return request<PipelineTemplate>(
      `/templates/${templateId}`,
      { method: 'PATCH', body: JSON.stringify(body) },
      csrf,
    )
  },
  copy(
    templateId: string,
    body: ApiSchemas['PipelineTemplateCopy'],
    csrf: string,
  ): Promise<PipelineTemplate> {
    return request<PipelineTemplate>(
      `/templates/${templateId}/copy`,
      { method: 'POST', body: JSON.stringify(body) },
      csrf,
    )
  },
  archive(
    templateId: string,
    expectedVersion: number,
    csrf: string,
  ): Promise<PipelineTemplate> {
    return request<PipelineTemplate>(
      `/templates/${templateId}/archive?expected_version=${expectedVersion}`,
      { method: 'POST' },
      csrf,
    )
  },
  save(
    templateId: string,
    body: ApiSchemas['PipelineDraftUpdate'],
    csrf: string,
  ): Promise<PipelineTemplate> {
    return request<PipelineTemplate>(
      `/templates/${templateId}/save`,
      { method: 'PUT', body: JSON.stringify(body) },
      csrf,
    )
  },
  saved(templateId: string): Promise<PipelineVersion | null> {
    return request<PipelineVersion | null>(`/templates/${templateId}/saved`)
  },
  attach(
    templateId: string,
    body: ApiSchemas['PipelineBindingCreate'],
    csrf: string,
  ): Promise<PipelineBinding> {
    return request<PipelineBinding>(
      `/templates/${templateId}/bindings`,
      { method: 'POST', body: JSON.stringify(body) },
      csrf,
    )
  },
  listVersions(templateId: string): Promise<PipelineVersion[]> {
    return request<PipelineVersion[]>(`/templates/${templateId}/versions`)
  },
  getVersion(versionId: string): Promise<PipelineVersion> {
    return request<PipelineVersion>(`/versions/${versionId}`)
  },
  createVersion(
    templateId: string,
    body: ApiSchemas['PipelineVersionCreate'],
    csrf: string,
  ): Promise<PipelineVersion> {
    return request<PipelineVersion>(
      `/templates/${templateId}/versions`,
      { method: 'POST', body: JSON.stringify(body) },
      csrf,
    )
  },
  updateDraft(
    templateId: string,
    body: ApiSchemas['PipelineDraftUpdate'],
    csrf: string,
  ): Promise<PipelineTemplate> {
    return request<PipelineTemplate>(
      `/templates/${templateId}/draft`,
      { method: 'PUT', body: JSON.stringify(body) },
      csrf,
    )
  },
  publishDraft(
    templateId: string,
    body: ApiSchemas['DraftPublish'],
    csrf: string,
  ): Promise<PipelineVersion> {
    return request<PipelineVersion>(
      `/templates/${templateId}/publish`,
      { method: 'POST', body: JSON.stringify(body) },
      csrf,
    )
  },
}

export const bindingsApi = {
  async preflight(
    bindingId: string,
    csrf: string,
    body: ApiSchemas['PreflightRequest'] = {},
  ): Promise<PreflightReport> {
    // ValidationReport serializes its preview fields at the response root.
    const check = () =>
      request<Omit<PreflightReport, 'preview'> & Record<string, unknown>>(
        `/bindings/${bindingId}/preflight`,
        { method: 'POST', body: JSON.stringify(body) },
        csrf,
      )
    let report = await check()
    const refresh = new Set(
      report.errors
        .filter((issue) => issue.code === 'harness_catalog_unverified')
        .map((issue) => issue.details?.harness_profile_id)
        .filter((id): id is string => typeof id === 'string'),
    )
    for (const id of refresh) {
      await request(
        `/harness_profiles/${encodeURIComponent(id)}/models/refresh?force=true`,
        { method: 'POST' },
        csrf,
      )
    }
    if (refresh.size) report = await check()
    const { ok, errors, warnings, execution_hash, ...preview } = report
    return { ok, errors, warnings, execution_hash, preview }
  },
  list(query: BindingQuery = {}): Promise<PipelineBinding[]> {
    const params = new URLSearchParams()
    if (query.projectId) params.set('project_id', query.projectId)
    if (query.includeArchived) params.set('include_archived', 'true')
    const search = params.toString()
    return request<PipelineBinding[]>(`/bindings${search ? `?${search}` : ''}`)
  },
  get(bindingId: string): Promise<PipelineBinding> {
    return request<PipelineBinding>(`/bindings/${bindingId}`)
  },
  create(
    versionId: string,
    body: ApiSchemas['PipelineBindingCreate'],
    csrf: string,
  ): Promise<PipelineBinding> {
    return request<PipelineBinding>(
      `/versions/${versionId}/bindings`,
      { method: 'POST', body: JSON.stringify(body) },
      csrf,
    )
  },
  update(
    bindingId: string,
    body: ApiSchemas['PipelineBindingUpdate'],
    csrf: string,
  ): Promise<PipelineBinding> {
    return request<PipelineBinding>(
      `/bindings/${bindingId}`,
      { method: 'PATCH', body: JSON.stringify(body) },
      csrf,
    )
  },
  archive(
    bindingId: string,
    expectedVersion: number,
    csrf: string,
  ): Promise<PipelineBinding> {
    return request<PipelineBinding>(
      `/bindings/${bindingId}/archive?expected_version=${expectedVersion}`,
      { method: 'POST' },
      csrf,
    )
  },
  resolved(bindingId: string): Promise<ResolvedSettings> {
    return request<ResolvedSettings>(`/bindings/${bindingId}/resolved`)
  },
  resolvedWithOverrides(
    bindingId: string,
    overrides: ApiSchemas['SettingsOverrides'],
    csrf: string,
  ): Promise<ResolvedSettings> {
    return request<ResolvedSettings>(
      `/bindings/${bindingId}/resolved`,
      { method: 'POST', body: JSON.stringify(overrides) },
      csrf,
    )
  },
}

export const presetsApi = {
  list(): Promise<PresetSummary[]> {
    return request<PresetSummary[]>('/presets')
  },
  copy(
    presetId: string,
    csrf: string,
    name?: string,
  ): Promise<PipelineTemplate> {
    return request<PipelineTemplate>(
      `/presets/${presetId}/copy`,
      {
        method: 'POST',
        body: JSON.stringify(name ? { name } : {}),
      },
      csrf,
    )
  },
}

export function describeBindingError(error: unknown): string {
  if (error instanceof ApiError) return error.body.message
  if (error instanceof Error) return error.message
  return 'Неизвестная ошибка'
}
