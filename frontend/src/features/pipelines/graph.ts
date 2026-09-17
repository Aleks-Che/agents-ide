import type { ApiSchemas } from '../../api/generated'
import type {
  Graph,
  GraphDocument,
  GraphEdge,
  GraphIssue,
  GraphNode,
  Schema,
} from '../../api/graphs'

export function object(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
}

export function documentFrom(
  draft: ApiSchemas['PipelineDraft'],
): GraphDocument {
  const raw = structuredClone(draft.graph ?? {})
  const edges = (Array.isArray(raw.edges) ? raw.edges : []).map(
    (value: unknown) => {
      const edge = object(value)
      const { from, to, from_node, to_node, edge_id, ...rest } = edge
      return {
        ...rest,
        ...((edge.id ?? edge_id) ? { id: String(edge.id ?? edge_id) } : {}),
        source: String(edge.source ?? from ?? from_node ?? ''),
        target: String(edge.target ?? to ?? to_node ?? ''),
      } as GraphEdge
    },
  )
  return {
    schema_version: draft.schema_version ?? '1.0.0',
    origin: draft.origin ?? 'local',
    required_features: draft.required_features ?? [],
    inputs: draft.inputs ?? {},
    settings: draft.settings ?? {},
    graph: {
      ...raw,
      nodes: (Array.isArray(raw.nodes) ? raw.nodes : []) as GraphNode[],
      edges,
    },
  }
}

export function nextId(prefix: string, ids: string[]) {
  for (let i = 1; ; i++)
    if (!ids.includes(`${prefix}_${i}`)) return `${prefix}_${i}`
}

export function edgeKey(edge: GraphEdge, index: number): string {
  return edge.id ?? `edge_${index}`
}

export function replaceEdge(
  graph: Graph,
  edited: GraphEdge & { id: string },
): Graph {
  return {
    ...graph,
    edges: graph.edges.map((edge, index) => {
      if (edgeKey(edge, index) !== edited.id) return edge
      const next: GraphEdge = { ...edited }
      // Implicit IDs are UI keys. Adding them to a legacy graph would change its hash.
      if (edge.id === undefined) delete next.id
      return next
    }),
  }
}

export function defaultValue(schema: Schema): unknown {
  if ('default' in schema) return structuredClone(schema.default)
  if ('const' in schema) return schema.const
  if (schema.enum) return schema.enum[0]
  const alternatives = schema.oneOf ?? schema.anyOf
  if (alternatives?.length) return defaultValue(alternatives[0])
  if (schema.type === 'object')
    return Object.fromEntries(
      (schema.required ?? []).map((key) => [
        key,
        defaultValue(schema.properties?.[key] ?? {}),
      ]),
    )
  if (schema.type === 'array')
    return Array.from({ length: schema.minItems ?? 0 }, () =>
      defaultValue(schema.items ?? {}),
    )
  if (schema.type === 'boolean') return false
  if (schema.type === 'integer' || schema.type === 'number')
    return schema.minimum ?? 0
  if (schema.type === 'null') return null
  return ''
}

export function createNode(
  type: string,
  schema: Schema,
  graph: Graph,
): GraphNode {
  const id = nextId(
    type.toLowerCase(),
    graph.nodes.map((n) => n.id),
  )
  const node = {
    ...object(defaultValue(schema)),
    id,
    type,
    position: {
      x: 70 + (graph.nodes.length % 3) * 250,
      y: 60 + Math.floor(graph.nodes.length / 3) * 160,
    },
  } as GraphNode
  if (type === 'Condition') node.expression = { const: true }
  return node
}

export function connectionError(
  graph: Graph,
  source: string,
  target: string,
  when?: string,
  except?: string,
): string | null {
  const from = graph.nodes.find((n) => n.id === source)
  const to = graph.nodes.find((n) => n.id === target)
  if (!from || !to) return 'Выберите оба узла.'
  if (from.type === 'End') return 'End не имеет выходов.'
  if (to.type === 'Start') return 'Start не имеет входов.'
  if (
    from.type === 'Condition' &&
    !['true', 'false', 'unknown'].includes(when ?? '')
  )
    return 'Выберите выход true, false или unknown.'
  if (
    graph.edges.some(
      (e, index) =>
        edgeKey(e, index) !== except &&
        e.source === source &&
        (from.type !== 'Condition' || e.when === when),
    )
  )
    return 'Параллельные выходы запрещены. Измените существующую связь.'
  return null
}

export function deleteNode(graph: Graph, id: string): Graph {
  return {
    ...graph,
    nodes: graph.nodes.filter((n) => n.id !== id),
    edges: graph.edges.filter((e) => e.source !== id && e.target !== id),
  }
}

export function duplicateNode(graph: Graph, id: string): GraphNode | null {
  const node = graph.nodes.find((n) => n.id === id)
  if (!node || node.type === 'Start') return null
  const copy = structuredClone(node)
  copy.id = nextId(
    node.type.toLowerCase(),
    graph.nodes.map((n) => n.id),
  )
  copy.position = {
    x: (node.position?.x ?? 0) + 50,
    y: (node.position?.y ?? 0) + 80,
  }
  // A duplicate must refer to its own result when the original did.
  const rewrite = (value: unknown): unknown =>
    typeof value === 'string'
      ? value.replaceAll(`steps.${id}.`, `steps.${copy.id}.`)
      : Array.isArray(value)
        ? value.map(rewrite)
        : value && typeof value === 'object'
          ? Object.fromEntries(
              Object.entries(value).map(([k, v]) => [k, rewrite(v)]),
            )
          : value
  return rewrite(copy) as GraphNode
}

export function switchExecutor(
  doc: GraphDocument,
  id: string,
  type: 'AgentTask' | 'LLMRequest',
): GraphDocument {
  const original = doc.graph.nodes.find((n) => n.id === id)!
  const config = { ...original.config }
  for (const key of [
    'model_selection',
    'model',
    'harness_profile_id',
    'harness_settings',
    'connection_id',
    'expected_kind',
  ])
    delete config[key]
  const role = typeof config.role === 'string' ? config.role : null
  const kind = type === 'AgentTask' ? 'agent' : 'llm'
  const roles = { ...doc.graph.roles }
  doc.graph.nodes.forEach((n) => {
    if (
      typeof n.config?.role === 'string' &&
      ['AgentTask', 'LLMRequest'].includes(n.type)
    )
      roles[n.config.role] ??= n.type === 'AgentTask' ? 'agent' : 'llm'
  })
  const settings = structuredClone(doc.settings)
  if (role) {
    // Preserve other nodes of a shared role by giving the converted node its own role.
    if (
      doc.graph.nodes.some(
        (n) => n.id !== id && n.config?.role === role && n.type !== type,
      )
    ) {
      config.role = nextId(`${role}_${kind}`, Object.keys(roles))
      roles[String(config.role)] = kind
    } else {
      roles[role] = kind
      delete settings.model_selections?.[role]
      delete settings.model_overrides?.[role]
      delete settings.role_assignments?.[role]
      delete settings.role_parameters?.[role]
    }
  }
  return {
    ...doc,
    settings,
    graph: {
      ...doc.graph,
      roles,
      nodes: doc.graph.nodes.map((n) =>
        n.id === id ? { ...n, type, config } : n,
      ),
    },
  }
}

export function issueLocation(
  issue: GraphIssue,
  graph: Graph,
): { node?: string; edge?: string } {
  if (issue.node_id || issue.edge_id)
    return { node: issue.node_id, edge: issue.edge_id }
  const path = issue.details?.path
  const parts = Array.isArray(path)
    ? path
    : typeof path === 'string'
      ? path.replaceAll('[', '.').replaceAll(']', '').split(/[./]/)
      : []
  const n = parts.indexOf('nodes'),
    e = parts.indexOf('edges')
  return {
    node: n >= 0 ? graph.nodes[Number(parts[n + 1])]?.id : undefined,
    edge:
      e >= 0 && graph.edges[Number(parts[e + 1])]
        ? edgeKey(graph.edges[Number(parts[e + 1])], Number(parts[e + 1]))
        : undefined,
  }
}

export function contextReferences(graph: Graph): string[] {
  const refs = [
    'project.id',
    'project.name',
    'project.path',
    'project.settings',
    'run.id',
    'run.cycle_id',
    'run.remaining_limits',
    ...[
      'current_plan_item_id',
      'scope',
      'plan_item_ids',
      'completed_items',
      'remaining_items',
      'mode',
      'feedback',
      'plan_feedback',
      'cycle_id',
    ].map((field) => `work.${field}`),
    ...['context', 'reports', 'diff', 'files', 'commands'].map(
      (field) => `artifacts.${field}`,
    ),
  ]
  const walk = (schema: Schema, path: string, depth: number) => {
    if (depth > 12) return
    refs.push(path)
    Object.entries(schema.properties ?? {}).forEach(([key, child]) =>
      walk(child, `${path}.${key}`, depth + 1),
    )
  }
  Object.entries(graph.input_schema?.properties ?? {}).forEach(([key, value]) =>
    walk(value, `input.${key}`, 0),
  )
  graph.nodes.forEach((n) => {
    for (const field of [
      'decision',
      'raw_result_ref',
      'validated_result',
      'evidence_manifest_id',
      'plan_item_ids',
      'execution_id',
      'attempt_id',
      'cycle_id',
    ])
      refs.push(`steps.${n.id}.latest.${field}`)
    const schema = object(n.config?.output_schema) as Schema
    Object.entries(schema.properties ?? {}).forEach(([key, value]) =>
      walk(value, `steps.${n.id}.latest.validated_result.${key}`, 0),
    )
  })
  return [...new Set(refs)]
}

export function downloadJson(value: unknown, name: string) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' }),
  )
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
