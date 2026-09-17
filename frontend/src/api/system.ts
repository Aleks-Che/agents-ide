import { request } from './client'
import type { ApiSchemas } from './generated'

export type ApplicationLogs = ApiSchemas['ApplicationLogs']
export type LogRole =
  'all' | 'launcher' | 'api' | 'worker' | 'start' | 'stop' | 'migrate' | 'auth'
export type LogLevel = 'INFO' | 'WARNING' | 'ERROR'
export type LogScope = 'current' | 'all'

export function applicationLogs(
  role: LogRole,
  level: LogLevel,
  scope: LogScope,
) {
  return request<ApplicationLogs>(
    `/system/logs?role=${role}&level=${level}&scope=${scope}`,
  )
}
