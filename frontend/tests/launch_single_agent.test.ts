import { describe, expect, it } from 'vitest'
import {
  buildSingleAgent,
  parseOptionalJsonObject,
  singleAgentNodes,
} from '../src/features/runs/launch_single_agent'

const resources = {
  groups: {
    agent: [
      {
        id: 'agent-group',
        name: 'heavy',
        archived: false,
        members: [
          { id: 'm1', member_index: 0, enabled: true, model_id: 'heavy-1' },
        ],
      },
    ],
    llm: [
      {
        id: 'llm-group',
        name: 'flash',
        archived: false,
        members: [
          { id: 'm2', member_index: 0, enabled: true, model_id: 'flash-1' },
        ],
      },
    ],
  },
  harnesses: [{ id: 'profile-1', name: 'p1', archived: false }],
  connections: [{ id: 'connection-1', name: 'c1', archived: false }],
}

describe('parseOptionalJsonObject', () => {
  it('returns null for empty text', () => {
    expect(parseOptionalJsonObject('', 'параметров')).toBeNull()
    expect(parseOptionalJsonObject('   ', 'параметров')).toBeNull()
  })

  it('parses valid JSON object', () => {
    expect(parseOptionalJsonObject('{"temperature": 0.7}', 'x')).toEqual({
      temperature: 0.7,
    })
  })

  it('rejects non-object JSON', () => {
    expect(() => parseOptionalJsonObject('[]', 'параметров')).toThrow(
      /должно быть JSON-объектом/,
    )
    expect(() => parseOptionalJsonObject('"foo"', 'параметров')).toThrow(
      /должно быть JSON-объектом/,
    )
  })

  it('rejects invalid JSON with original cause attached', () => {
    let thrown: unknown
    try {
      parseOptionalJsonObject('not json', 'параметров')
    } catch (error) {
      thrown = error
    }
    expect(thrown).toBeInstanceOf(Error)
    expect((thrown as Error & { cause?: unknown }).cause).toBeInstanceOf(
      SyntaxError,
    )
    expect((thrown as Error).message).toMatch(/невалидный JSON/)
  })
})

describe('buildSingleAgent', () => {
  it('lists executable nodes separately when a role is shared and ignores unused declarations', () => {
    expect(
      singleAgentNodes({
        roles: { unused: 'llm' },
        nodes: [
          { id: 'first', type: 'LLMRequest', config: { role: 'dev' } },
          { id: 'second', type: 'LLMRequest', config: { role: 'dev' } },
          { id: 's', type: 'Start' },
        ],
      }),
    ).toEqual([
      { id: 'first', role: 'dev', kind: 'llm' },
      { id: 'second', role: 'dev', kind: 'llm' },
    ])
  })

  it('pins the chosen node and checks direct/group resource kinds', () => {
    const node = { id: 'second', role: 'dev', kind: 'llm' as const }
    expect(
      buildSingleAgent('dev', { kind: null }, '{}', resources, node),
    ).toMatchObject({ node_id: 'second' })
    expect(() =>
      buildSingleAgent(
        'dev',
        { kind: 'group', group_id: 'agent-group' },
        '{}',
        resources,
        node,
      ),
    ).toThrow('недоступна')
    expect(() =>
      buildSingleAgent(
        'dev',
        { kind: 'direct', harness_profile_id: 'profile-1', model_id: 'm' },
        '{}',
        resources,
        node,
      ),
    ).toThrow('Тип ресурса')
    expect(() =>
      buildSingleAgent(
        'dev',
        {
          kind: 'direct',
          harness_profile_id: 'profile-1',
          provider_connection_id: 'connection-1',
          model_id: 'm',
        },
        '{}',
        resources,
      ),
    ).toThrow('Тип ресурса')
  })
  it('requires a role', () => {
    expect(() => buildSingleAgent('', { kind: null }, '{}', resources)).toThrow(
      /Выберите роль/,
    )
  })

  it('returns base spec when no selection is provided', () => {
    const spec = buildSingleAgent('dev', { kind: null }, '', resources)
    expect(spec.role).toBe('dev')
    expect(spec.parameters == null).toBe(true)
  })

  it('treats explicit {} as empty parameters object', () => {
    const spec = buildSingleAgent('dev', { kind: null }, '{}', resources)
    expect(spec.parameters).toEqual({})
  })

  it('parses optional parameters', () => {
    expect(
      buildSingleAgent(
        'dev',
        { kind: null },
        '{"temperature": 0.5, "top_p": 0.9}',
        resources,
      ),
    ).toEqual({
      role: 'dev',
      parameters: { temperature: 0.5, top_p: 0.9 },
    })
  })

  it('rejects invalid parameters JSON', () => {
    expect(() =>
      buildSingleAgent('dev', { kind: null }, '{not json', resources),
    ).toThrow(/невалидный JSON/)
  })

  it('rejects missing direct fields', () => {
    expect(() =>
      buildSingleAgent('dev', { kind: 'direct' }, '{}', resources),
    ).toThrow(/Укажите ID модели/)
  })

  it('rejects direct selection without a known resource', () => {
    expect(() =>
      buildSingleAgent(
        'dev',
        { kind: 'direct', model_id: 'm', harness_profile_id: 'unknown' },
        '{}',
        resources,
      ),
    ).toThrow(/Harness-профиль недоступен/)
  })

  it('rejects missing group id', () => {
    expect(() =>
      buildSingleAgent('dev', { kind: 'group' }, '{}', resources),
    ).toThrow(/Выберите группу/)
  })

  it('rejects unknown group id', () => {
    expect(() =>
      buildSingleAgent(
        'dev',
        { kind: 'group', group_id: 'missing' },
        '{}',
        resources,
      ),
    ).toThrow(/Группа моделей недоступна/)
  })

  it('rejects archived resources', () => {
    expect(() =>
      buildSingleAgent(
        'dev',
        {
          kind: 'direct',
          model_id: 'm',
          harness_profile_id: 'profile-archived',
        },
        '{}',
        {
          ...resources,
          harnesses: [{ id: 'profile-archived', name: 'old', archived: true }],
        },
      ),
    ).toThrow(/Harness-профиль недоступен/)
  })

  it('builds a direct agent selection', () => {
    const spec = buildSingleAgent(
      'dev',
      { kind: 'direct', model_id: ' m ', harness_profile_id: 'profile-1' },
      '{}',
      resources,
    )
    expect(spec).toMatchObject({
      role: 'dev',
      selection: {
        kind: 'direct',
        model_id: 'm',
        harness_profile_id: 'profile-1',
      },
    })
  })

  it('builds a direct LLM selection', () => {
    const spec = buildSingleAgent(
      'verifier',
      {
        kind: 'direct',
        model_id: 'flash-2',
        provider_connection_id: 'connection-1',
      },
      '{}',
      resources,
    )
    expect(spec).toMatchObject({
      role: 'verifier',
      selection: {
        kind: 'direct',
        model_id: 'flash-2',
        provider_connection_id: 'connection-1',
      },
    })
  })

  it('builds a group selection', () => {
    const spec = buildSingleAgent(
      'verifier',
      { kind: 'group', group_id: 'llm-group' },
      '{}',
      resources,
    )
    expect(spec).toMatchObject({
      role: 'verifier',
      selection: { kind: 'group', group_id: 'llm-group' },
    })
  })

  it('rejects direct selection missing both resource and id', () => {
    expect(() =>
      buildSingleAgent(
        'dev',
        { kind: 'direct', model_id: 'm' },
        '{}',
        resources,
      ),
    ).toThrow(/harness-профиль или LLM-подключение/)
  })
})
