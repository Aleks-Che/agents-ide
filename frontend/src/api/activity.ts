import { request } from './client'
import type { ApiSchemas } from './generated'

export type ActivityStatus = ApiSchemas['ActivityStatus']
export type SidebarActivity = ApiSchemas['SidebarActivity']

export function getSidebarActivity(): Promise<SidebarActivity> {
  return request<SidebarActivity>('/sidebar/activity')
}
