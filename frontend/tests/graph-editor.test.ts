import { describe, expect, it } from 'vitest'
import {
  connectionError,
  contextReferences,
  deleteNode,
  documentFrom,
  duplicateNode,
  edgeKey,
  issueLocation,
  replaceEdge,
  switchExecutor,
} from '../src/features/pipelines/graph'
import {
  remapResources,
  resourceReferences,
} from '../src/features/pipelines/transfer'
import { confirmedPlans } from '../src/features/planning/confirmed_plans'
import type { PlanningJobView } from '../src/api/planning'

const document = () =>
  documentFrom({
    graph: {
      nodes: [
        { id: 'start', type: 'Start' },
        {
          id: 'implement',
          type: 'AgentTask',
          config: {
            role: 'author',
            prompt: '{{ steps.implement.latest.decision }}',
          },
        },
        {
          id: 'verify',
          type: 'LLMRequest',
          config: {
            role: 'verifier',
            prompt: 'check',
            json_processing: { strip_thinking_tags: true, extract_json: true },
            output_schema: {
              type: 'object',
              properties: { verdict: { type: 'string' } },
            },
          },
        },
        {
          id: 'decision',
          type: 'Condition',
          expression: { ref: 'steps.verify.latest.decision' },
        },
        { id: 'end', type: 'End' },
      ],
      edges: [
        { id: 'start_edge', from: 'start', to: 'implement' },
        {
          edge_id: 'decision_true',
          from_node: 'decision',
          to_node: 'end',
          when: 'true',
        },
      ],
      input_schema: {
        type: 'object',
        properties: { task: { type: 'string' } },
      },
    },
    settings: {
      model_selections: {
        author: { kind: 'group', group_id: 'remote-group' },
        verifier: {
          kind: 'direct',
          provider_connection_id: 'remote-llm',
          model_id: 'm',
        },
      },
    },
  })

describe('graph editing invariants', () => {
  it('removes LLM JSON processing when switching to an agent', () => {
    const converted = switchExecutor(document(), 'verify', 'AgentTask')
    expect(
      converted.graph.nodes.find((node) => node.id === 'verify')?.config,
    ).not.toHaveProperty('json_processing')
  })
  it('keeps implicit edge IDs out of serialized graphs, including after a label edit', () => {
    const doc = documentFrom({
      graph: {
        nodes: [
          { id: 's', type: 'Start' },
          { id: 'e', type: 'End' },
        ],
        edges: [{ from: 's', to: 'e' }],
      },
    })
    const edge = doc.graph.edges[0]
    expect(edgeKey(edge, 0)).toBe('edge_0')
    expect(connectionError(doc.graph, 's', 'e', undefined, 'edge_0')).toBeNull()
    const edited = replaceEdge(doc.graph, {
      ...edge,
      id: 'edge_0',
      label: 'Continue',
    })
    expect(JSON.parse(JSON.stringify(edited.edges))).toEqual([
      { source: 's', target: 'e', label: 'Continue' },
    ])
    expect(duplicateNode(doc.graph, 'e')?.type).toBe('End')
  })
  it('round-trips aliases, metadata and imported provenance without trusting file hashes', () => {
    const draft = document()
    draft.origin = 'imported'
    draft.graph.visual = { viewport: { x: 10, y: 20, zoom: 0.8 } }
    const next = documentFrom(draft)
    expect(next).toEqual(draft)
    expect(next.graph.edges[1]).toEqual({
      id: 'decision_true',
      source: 'decision',
      target: 'end',
      when: 'true',
    })
    expect('execution_hash' in next).toBe(false)
  })
  it('rejects parallel ordinary and same-branch edges but permits other condition outputs', () => {
    const { graph } = document()
    expect(connectionError(graph, 'start', 'verify')).toContain('Параллельные')
    expect(connectionError(graph, 'decision', 'implement', 'true')).toContain(
      'Параллельные',
    )
    expect(connectionError(graph, 'decision', 'implement', 'false')).toBeNull()
    expect(connectionError(graph, 'end', 'implement')).toContain('End')
    expect(connectionError(graph, 'decision', 'start', 'false')).toContain(
      'Start',
    )
    expect(
      connectionError(graph, 'decision', 'implement', undefined),
    ).toContain('true')
    expect(
      connectionError(graph, 'start', 'verify', undefined, 'start_edge'),
    ).toBeNull()
  })
  it('duplicates a node without outgoing edges and removes incident edges on deletion', () => {
    const { graph } = document()
    const duplicate = duplicateNode(graph, 'implement')!
    expect(duplicate.id).not.toBe('implement')
    expect(duplicate.config?.prompt).toContain(
      `steps.${duplicate.id}.latest.decision`,
    )
    duplicate.config!.prompt = 'changed'
    expect(graph.nodes[1].config?.prompt).toContain('steps.implement')
    expect(duplicateNode(graph, 'start')).toBeNull()
    const deleted = deleteNode(graph, 'implement')
    expect(
      deleted.edges.some(
        (e) => e.target === 'implement' || e.source === 'implement',
      ),
    ).toBe(false)
    expect(graph.edges).toHaveLength(2)
  })
  it('changes executor without retaining incompatible role routing or changing other shared nodes', () => {
    const doc = document()
    doc.graph.nodes[1].config!.model_selection = {
      kind: 'direct',
      model_id: 'm',
      harness_profile_id: 'h',
    }
    const converted = switchExecutor(doc, 'implement', 'LLMRequest')
    expect(converted.graph.nodes[1].config).toEqual({
      role: 'author',
      prompt: doc.graph.nodes[1].config?.prompt,
    })
    expect(converted.graph.roles?.author).toBe('llm')
    expect(converted.settings.model_selections?.author).toBeUndefined()
    doc.graph.nodes.push({
      id: 'second',
      type: 'AgentTask',
      config: { role: 'author', prompt: 'second' },
    })
    const shared = switchExecutor(doc, 'implement', 'LLMRequest')
    expect(shared.graph.nodes[1].config?.role).not.toBe('author')
    expect(shared.settings.model_selections?.author).toEqual({
      kind: 'group',
      group_id: 'remote-group',
    })
  })
  it('offers only declared context and structured result references', () => {
    const refs = contextReferences(document().graph)
    expect(refs).toContain('input.task')
    expect(refs).toContain('steps.verify.latest.validated_result.verdict')
    expect(refs).toContain('work.current_plan_item_id')
    expect(refs).not.toContain('steps.verify.latest.body_text')
    expect(refs).not.toContain('project.workspace_path')
  })
  it('locates schema errors on a specific node or edge', () => {
    const { graph } = document()
    expect(
      issueLocation(
        {
          code: 'invalid',
          message: 'bad',
          details: { path: ['edges', 1, 'loop'] },
        },
        graph,
      ).edge,
    ).toBe('decision_true')
    expect(
      issueLocation(
        { code: 'invalid', message: 'bad', node_id: 'verify' },
        graph,
      ).node,
    ).toBe('verify')
  })
})

describe('portable resource references', () => {
  it('infers group kind from role users and maps routing without replacing prompts or inputs', () => {
    const doc = document()
    doc.inputs.note = 'remote-llm'
    doc.graph.nodes[1].config!.harness_profile_id = 'legacy-harness'
    expect(resourceReferences(doc)).toEqual(
      expect.arrayContaining([
        {
          key: 'group:agent:remote-group',
          id: 'remote-group',
          type: 'group',
          kind: 'agent',
        },
        {
          key: 'connection:llm:remote-llm',
          id: 'remote-llm',
          type: 'connection',
          kind: 'llm',
        },
        {
          key: 'harness:agent:legacy-harness',
          id: 'legacy-harness',
          type: 'harness',
          kind: 'agent',
        },
      ]),
    )
    const next = remapResources(doc, {
      'group:agent:remote-group': 'local-group',
      'connection:llm:remote-llm': 'local-llm',
      'harness:agent:legacy-harness': 'local-harness',
    })
    expect(next.settings.model_selections?.author).toEqual({
      kind: 'group',
      group_id: 'local-group',
    })
    expect(next.graph.nodes[1].config?.harness_profile_id).toBe('local-harness')
    expect(next.inputs.note).toBe('remote-llm')
    expect(doc.settings.model_selections?.verifier).toMatchObject({
      provider_connection_id: 'remote-llm',
    })
  })
})

it('Council selector excludes drafts, other projects/chats and obsolete revisions', () => {
  const job = {
    id: 'j',
    project_id: 'p',
    chat_id: 'c',
    state: 'confirmed',
    revisions: [
      {
        revision_number: 1,
        readiness: 'ready',
        confirmed_at: 'today',
        confirmation_hash: 'hash',
      },
    ],
  } as PlanningJobView
  expect(confirmedPlans([job], 'p', 'c')[0].source).toEqual({
    job_id: 'j',
    revision_number: 1,
    confirmation_hash: 'hash',
  })
  for (const changed of [
    { state: 'awaiting_confirmation' },
    { project_id: 'other' },
    { chat_id: 'other' },
    { revisions: [] },
    {
      revisions: [
        ...job.revisions!,
        { revision_number: 2, readiness: 'ready' },
      ],
    },
  ]) {
    expect(
      confirmedPlans([{ ...job, ...changed } as PlanningJobView], 'p', 'c'),
    ).toEqual([])
  }
})
