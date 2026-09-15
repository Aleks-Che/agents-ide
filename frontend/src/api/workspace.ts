import { request } from './client'
import type { WorkspaceProbeResult } from './projects'

export function probeWorkspace(
  path: string,
  csrf: string,
): Promise<WorkspaceProbeResult> {
  return request<WorkspaceProbeResult>(
    '/workspace/probe',
    {
      method: 'POST',
      body: JSON.stringify({ path }),
    },
    csrf,
  )
}

export interface WorkspaceSummary {
  enteredPath: string
  normalizedPath: string
  gitRootPath: string | null
  gitHeadSha: string | null
  gitDefaultBranch: string | null
  gitDirty: boolean
  isGitRepo: boolean
}

export function summariseProbe(result: WorkspaceProbeResult): WorkspaceSummary {
  const git = (result['git'] as Record<string, unknown> | undefined) ?? {}
  const rootPath = (git['root_path'] as string | null | undefined) ?? null
  return {
    enteredPath: (result['entered_path'] as string | undefined) ?? '',
    normalizedPath: (result['normalized_path'] as string | undefined) ?? '',
    gitRootPath: rootPath,
    gitHeadSha: (git['head_sha'] as string | null | undefined) ?? null,
    gitDefaultBranch:
      (git['default_branch'] as string | null | undefined) ?? null,
    gitDirty: Boolean(git['dirty']),
    isGitRepo: rootPath !== null,
  }
}
