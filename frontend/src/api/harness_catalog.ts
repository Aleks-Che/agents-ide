import type { PreflightIssue } from './bindings'
import { request } from './client'

export async function refreshUnverifiedHarnessCatalogs(
  issues: PreflightIssue[],
  csrf: string,
): Promise<boolean> {
  const profiles = new Set(
    issues
      .filter((issue) => issue.code === 'harness_catalog_unverified')
      .map((issue) => issue.details?.harness_profile_id)
      .filter((id): id is string => typeof id === 'string' && id.length > 0),
  )
  for (const id of profiles) {
    await request(
      `/harness_profiles/${encodeURIComponent(id)}/models/refresh?force=true`,
      { method: 'POST' },
      csrf,
    )
  }
  return profiles.size > 0
}
