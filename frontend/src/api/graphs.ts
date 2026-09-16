import { request } from './client'
import type { ApiSchemas } from './generated'

export interface Schema {
  type?: string | string[]
  title?: string
  description?: string
  properties?: Record<string, Schema>
  required?: string[]
  additionalProperties?: boolean | Schema
  items?: Schema
  enum?: unknown[]
  const?: unknown
  default?: unknown
  anyOf?: Schema[]
  oneOf?: Schema[]
  $ref?: string
  minimum?: number
  maximum?: number
  minLength?: number
  maxLength?: number
  minItems?: number
  maxItems?: number
  pattern?: string
  [key: string]: unknown
}

export interface GraphNode {
  id: string
  type: string
  label?: string
  position?: { x: number; y: number }
  config?: Record<string, unknown>
  expression?: Record<string, unknown>
  [key: string]: unknown
}
export interface GraphEdge {
  id?: string
  source: string
  target: string
  when?: string
  label?: string
  loop?: { id: string; max_iterations: number; scope?: 'item' | 'run' }
  assignments?: Record<string, Record<string, unknown>>
  [key: string]: unknown
}
export interface Graph {
  nodes: GraphNode[]
  edges: GraphEdge[]
  roles?: Record<string, 'agent' | 'llm'>
  input_schema?: Schema
  visual?: Record<string, unknown>
  [key: string]: unknown
}
export type GraphDocument = Required<
  Pick<
    ApiSchemas['PipelineDraft'],
    'schema_version' | 'origin' | 'required_features' | 'inputs' | 'settings'
  >
> & { graph: Graph }

export interface NodeSchemas {
  schema_version: string
  nodes: Record<string, Schema>
  graph: Schema
  components: { schemas: Record<string, Schema> }
  limits: Record<string, number>
  supported_features: string[]
  ast: { max_depth: number; operators: string[]; reference_roots: string[] }
}
export interface GraphIssue {
  code: string
  message: string
  node_id?: string
  edge_id?: string
  details?: Record<string, unknown>
}
export interface GraphReport {
  ok: boolean
  errors: GraphIssue[]
  warnings: GraphIssue[]
  graph_hash?: string | null
  execution_hash?: string | null
  features: string[]
  body?: ApiSchemas['PipelineDraft'] | null
}
export interface Capabilities {
  engine_version: string
  supported_schema: string[]
  supported_graph_features: string[]
  adapter_capabilities: string
}

export function validationBody(doc: GraphDocument) {
  return {
    graph: doc.graph,
    inputs: doc.inputs,
    settings: doc.settings,
    schema_version: doc.schema_version,
    required_features: doc.required_features,
  }
}
export const graphsApi = {
  schemas: () => request<NodeSchemas>('/schema/nodes'),
  capabilities: () => request<Capabilities>('/capabilities'),
  validate: (doc: GraphDocument, csrf: string) =>
    request<GraphReport>(
      '/graphs/validate',
      {
        method: 'POST',
        body: JSON.stringify(validationBody(doc)),
      },
      csrf,
    ),
  import: (body: unknown, csrf: string) =>
    request<GraphReport>(
      '/graphs/import',
      {
        method: 'POST',
        body: JSON.stringify(body),
      },
      csrf,
    ),
  export: (doc: GraphDocument, csrf: string) =>
    request<Record<string, unknown>>(
      '/graphs/export',
      {
        method: 'POST',
        body: JSON.stringify(validationBody(doc)),
      },
      csrf,
    ),
}
