import type { ApiSchemas } from '../../api/generated'
import type { BindingSelectionDraft } from '../bindings/utils'

export interface LaunchResourceGroup {
  id: string
  name: string
  archived: boolean
  members: Array<{
    id: string
    member_index: number
    enabled: boolean
    model_id: string
  }>
}

export interface LaunchResources {
  groups: {
    agent: LaunchResourceGroup[]
    llm: LaunchResourceGroup[]
  }
  harnesses: Array<{ id: string; name: string; archived: boolean }>
  connections: Array<{ id: string; name: string; archived: boolean }>
}

export interface SingleAgentNode {
  id: string
  role: string
  kind: 'agent' | 'llm'
}

export function singleAgentNodes(
  graph: Record<string, unknown>,
): SingleAgentNode[] {
  if (!Array.isArray(graph.nodes)) return []
  return graph.nodes.flatMap((raw: unknown) => {
    if (!raw || typeof raw !== 'object') return []
    const node = raw as Record<string, unknown>
    const config = node.config as Record<string, unknown> | undefined
    if (typeof node.id !== 'string' || typeof config?.role !== 'string')
      return []
    if (node.type !== 'AgentTask' && node.type !== 'LLMRequest') return []
    return [
      {
        id: node.id,
        role: config.role,
        kind: node.type === 'AgentTask' ? ('agent' as const) : ('llm' as const),
      },
    ]
  })
}

export function buildSingleAgent(
  role: string,
  selection: BindingSelectionDraft,
  parametersText: string,
  resources: LaunchResources,
  node?: SingleAgentNode,
): ApiSchemas['SingleAgentSpec'] {
  if (!role) throw new Error('Выберите роль для одиночного агента.')
  const parameters = parseOptionalJsonObject(
    parametersText,
    'параметров модели',
  )
  const base: ApiSchemas['SingleAgentSpec'] = {
    role,
    ...(node ? { node_id: node.id } : {}),
    parameters: parameters ?? null,
  }
  if (!selection.kind) return base
  if (selection.kind === 'group') {
    if (!selection.group_id) throw new Error('Выберите группу моделей.')
    const exists = (
      node
        ? resources.groups[node.kind]
        : resources.groups.agent.concat(resources.groups.llm)
    ).some((group) => group.id === selection.group_id && !group.archived)
    if (!exists) throw new Error('Группа моделей недоступна.')
    return {
      ...base,
      selection: { kind: 'group', group_id: selection.group_id },
    }
  }
  const modelId = selection.model_id?.trim()
  if (!modelId) throw new Error('Укажите ID модели.')
  if (
    (selection.harness_profile_id && selection.provider_connection_id) ||
    (node?.kind === 'agent' && selection.provider_connection_id) ||
    (node?.kind === 'llm' && selection.harness_profile_id)
  ) {
    throw new Error('Тип ресурса не соответствует выбранному узлу.')
  }
  if (selection.harness_profile_id) {
    if (
      !resources.harnesses.some(
        (profile) =>
          profile.id === selection.harness_profile_id && !profile.archived,
      )
    ) {
      throw new Error('Harness-профиль недоступен.')
    }
    return {
      ...base,
      selection: {
        kind: 'direct',
        model_id: modelId,
        harness_profile_id: selection.harness_profile_id,
      },
    }
  }
  if (selection.provider_connection_id) {
    if (
      !resources.connections.some(
        (conn) =>
          conn.id === selection.provider_connection_id && !conn.archived,
      )
    ) {
      throw new Error('LLM-подключение недоступно.')
    }
    return {
      ...base,
      selection: {
        kind: 'direct',
        model_id: modelId,
        provider_connection_id: selection.provider_connection_id,
      },
    }
  }
  throw new Error('Укажите harness-профиль или LLM-подключение.')
}

export function parseOptionalJsonObject(
  text: string,
  fieldLabel: string,
): Record<string, unknown> | null {
  const trimmed = text.trim()
  if (!trimmed) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(trimmed)
  } catch (error) {
    throw new Error(
      `Поле ${fieldLabel} содержит невалидный JSON: ${
        error instanceof Error ? error.message : 'parse error'
      }`,
      { cause: error },
    )
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error(`Поле ${fieldLabel} должно быть JSON-объектом.`)
  }
  return parsed as Record<string, unknown>
}
