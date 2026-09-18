import { describe, expect, it } from 'vitest'
import type { EventEnvelope } from '../src/api/runs'
import { stageOutput } from '../src/features/runs/stage_output'

function tool(
  sequence: number,
  payload: Record<string, unknown> = {},
): EventEnvelope {
  return {
    sequence,
    run_id: 'run',
    node_id: 'agent',
    step_execution_id: 'execution',
    step_attempt_id: 'attempt',
    type: 'agent.tool_call',
    payload,
    occurred_at: 1,
    persisted_at: 1,
    worker_generation: 1,
  }
}

describe('stage tool output', () => {
  it('compacts four identical calls into one entry with a call count', () => {
    const output = stageOutput(
      Array.from({ length: 4 }, (_, index) => tool(index + 1)),
    )
    expect(output).toHaveLength(1)
    expect(output[0]).toMatchObject({ text: 'Инструмент', tool: { count: 4 } })
  })

  it('counts calls after resolving their latest status, without counting updates', () => {
    const output = stageOutput([
      tool(1, { call_id: 'a', tool: 'read', status: 'running' }),
      tool(2, { call_id: 'b', tool: 'read', status: 'running' }),
      tool(3, { call_id: 'a', tool: 'read', status: 'completed' }),
      tool(4, { call_id: 'b', tool: 'read', status: 'completed' }),
    ])
    expect(output).toHaveLength(1)
    expect(output[0]).toMatchObject({
      text: 'read · завершён',
      tool: { count: 2 },
    })
  })

  it('keeps different tool results separate, including an updated error', () => {
    const output = stageOutput([
      tool(1, { call_id: 'a', tool: 'read', status: 'running' }),
      tool(2, { call_id: 'b', tool: 'read', status: 'running' }),
      tool(3, { call_id: 'b', tool: 'read', status: 'failed' }),
      tool(4, { tool: 'write' }),
    ])
    expect(output.map((entry) => entry.text)).toEqual([
      'read · выполняется',
      'read · ошибка',
      'write',
    ])
    expect(output.map((entry) => entry.tool?.count)).toEqual([1, 1, 1])
  })

  it('preserves messages and attempt boundaries between identical tools', () => {
    const output = stageOutput([
      tool(1),
      {
        ...tool(2),
        type: 'agent.message_delta',
        payload: { text: 'Checking' },
      },
      tool(3),
      { ...tool(4), type: 'agent.user_message', payload: { text: 'Continue' } },
      tool(5),
      { ...tool(6), step_attempt_id: 'next-attempt' },
    ])
    expect(output).toHaveLength(6)
    expect(output[1].text).toBe('Checking')
    expect(output[3].text).toContain('Continue')
    expect(
      output.filter((entry) => entry.tool).map((entry) => entry.tool?.count),
    ).toEqual([1, 1, 1, 1])
  })
})
