import { describe, expect, it } from 'vitest'
import type { EventEnvelope } from '../src/api/runs'
import {
  EVENT_BYTES,
  mergeEvents,
  matchesEvent,
  selectedEdge,
} from '../src/features/runs/observation'

const event = (sequence: number, text = '') =>
  ({
    sequence,
    run_id: 'r',
    type: 'agent.message_delta',
    node_id: 'n',
    payload: { text },
    occurred_at: 1,
    persisted_at: 1,
    worker_generation: 1,
  }) as EventEnvelope

describe('bounded Run observation', () => {
  it('deduplicates replay and keeps the latest records in a count and byte budget', () => {
    const rows = Array.from({ length: 1000 }, (_, i) => event(i + 1))
    const window = mergeEvents(rows, [event(999), event(1000)])
    expect(window).toHaveLength(400)
    expect(window[0].sequence).toBe(1000)
    expect(window.at(-1)?.sequence).toBe(601)
    const large = mergeEvents(
      [],
      rows.map((row) => event(row.sequence, 'x'.repeat(16000))),
    )
    expect(large.length).toBeLessThan(20)
    expect(
      large.reduce((sum, row) => sum + JSON.stringify(row).length * 2, 0),
    ).toBeLessThanOrEqual(EVENT_BYTES)
  })
  it('filters messages and nodes without interpreting text as markup', () => {
    const row = event(1, '<img src=x onerror=alert(1)>')
    expect(matchesEvent(row, 'messages', 'n')).toBe(true)
    expect(matchesEvent(row, 'tools', 'n')).toBe(false)
    expect(matchesEvent(row, 'messages', 'other')).toBe(false)
  })
  it('uses persisted transition identity and distinguishes legacy conditional edges', () => {
    const edge = {
      id: 'ui_0',
      source: 'condition',
      target: 'end',
      when: 'true',
    }
    expect(
      selectedEdge(edge, {
        source_node_id: 'condition',
        target: 'end',
        reason: 'true',
      }),
    ).toBe(true)
    expect(
      selectedEdge(edge, {
        source_node_id: 'condition',
        target: 'end',
        reason: 'false',
      }),
    ).toBe(false)
    expect(selectedEdge(edge, { edge_id: 'different' })).toBe(false)
  })
})
