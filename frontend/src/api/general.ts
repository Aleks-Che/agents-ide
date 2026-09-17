import { request } from './client'
import type { ApiSchemas } from './generated'

export type GeneralSettings = ApiSchemas['GeneralSettings']
export type CommitMessageSettings = ApiSchemas['CommitMessageSettings']

export const generalApi = {
  get: () => request<GeneralSettings>('/settings/general'),
  save: (body: GeneralSettings, csrf: string) =>
    request<GeneralSettings>(
      '/settings/general',
      { method: 'PUT', body: JSON.stringify(body) },
      csrf,
    ),
}
