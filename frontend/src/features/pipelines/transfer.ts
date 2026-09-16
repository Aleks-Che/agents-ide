import type { GraphDocument } from '../../api/graphs'
import { graphRoles } from '../bindings/utils'
import { object } from './graph'

export interface ResourceReference {
  key: string
  id: string
  type: 'group' | 'harness' | 'connection'
  kind: 'agent' | 'llm'
}

export function resourceReferences(doc: GraphDocument): ResourceReference[] {
  const result: ResourceReference[] = []
  const roles = graphRoles(doc.graph)
  const add = (
    type: ResourceReference['type'],
    id: unknown,
    kind: 'agent' | 'llm',
  ) => {
    if (typeof id !== 'string' || !id) return
    const key = `${type}:${kind}:${id}`
    if (!result.some((ref) => ref.key === key))
      result.push({ key, id, type, kind })
  }
  const selection = (value: unknown, kind: 'agent' | 'llm') => {
    const s = object(value)
    add('group', s.group_id, kind)
    add('harness', s.harness_profile_id, 'agent')
    add('connection', s.provider_connection_id, 'llm')
  }
  Object.entries(doc.settings.model_selections ?? {}).forEach(([role, value]) =>
    selection(
      value,
      roles[role]?.kind ?? ('harness_profile_id' in value ? 'agent' : 'llm'),
    ),
  )
  Object.entries(doc.settings.role_assignments ?? {}).forEach(([role, id]) =>
    add(
      roles[role]?.kind === 'llm' ? 'connection' : 'harness',
      id,
      roles[role]?.kind ?? 'agent',
    ),
  )
  doc.graph.nodes.forEach((node) => {
    const c = object(node.config)
    selection(c.model_selection, node.type === 'AgentTask' ? 'agent' : 'llm')
    add('harness', c.harness_profile_id, 'agent')
    add('connection', c.connection_id, 'llm')
  })
  return result
}

export function remapResources(
  doc: GraphDocument,
  mapping: Record<string, string>,
): GraphDocument {
  const next = structuredClone(doc)
  const roles = graphRoles(doc.graph)
  const map = (
    type: ResourceReference['type'],
    id: unknown,
    kind: 'agent' | 'llm',
  ) => (typeof id === 'string' ? (mapping[`${type}:${kind}:${id}`] ?? id) : id)
  const selection = (value: unknown, kind: 'agent' | 'llm') => {
    const s = object(value)
    if (s.group_id) s.group_id = map('group', s.group_id, kind)
    if (s.harness_profile_id)
      s.harness_profile_id = map('harness', s.harness_profile_id, 'agent')
    if (s.provider_connection_id)
      s.provider_connection_id = map(
        'connection',
        s.provider_connection_id,
        'llm',
      )
  }
  Object.entries(next.settings.model_selections ?? {}).forEach(
    ([role, value]) =>
      selection(
        value,
        roles[role]?.kind ?? ('harness_profile_id' in value ? 'agent' : 'llm'),
      ),
  )
  Object.entries(next.settings.role_assignments ?? {}).forEach(([role, id]) => {
    const kind = roles[role]?.kind ?? 'agent'
    next.settings.role_assignments![role] = String(
      map(kind === 'agent' ? 'harness' : 'connection', id, kind),
    )
  })
  next.graph.nodes.forEach((node) => {
    const c = object(node.config)
    selection(c.model_selection, node.type === 'AgentTask' ? 'agent' : 'llm')
    if (c.harness_profile_id)
      c.harness_profile_id = map('harness', c.harness_profile_id, 'agent')
    if (c.connection_id)
      c.connection_id = map('connection', c.connection_id, 'llm')
  })
  return next
}
