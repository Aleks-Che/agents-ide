import { useQuery } from '@tanstack/react-query'
import {
  connectionsApi,
  groupsApi,
  harnessApi,
  type HarnessProfile,
  type ModelGroup,
  type ProviderConnection,
} from '../../api/settings'

export interface EditorResources {
  groups: ModelGroup[]
  harnesses: HarnessProfile[]
  connections: ProviderConnection[]
}

export function useEditorResources() {
  return useQuery({
    queryKey: ['graph_editor_resources'],
    queryFn: async (): Promise<EditorResources> => {
      const [groups, harnesses, connections] = await Promise.all([
        groupsApi.list({ includeArchived: true }),
        harnessApi.list({ includeArchived: true }),
        connectionsApi.list({ includeArchived: true }),
      ])
      return { groups, harnesses, connections }
    },
  })
}
