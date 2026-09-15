import { describe, expect, it } from 'vitest'

import {
  CANDIDATE_STATE_LABELS,
  MODEL_GROUP_EVENT_TYPES,
  type SelectionSummary,
} from '../src/api/runs'
import {
  hasSelectionSummary,
  visitCurrentTarget,
} from '../src/features/runs/group_summary_helpers'

const summary: SelectionSummary = {
  group_changes_apply_only_to_new_runs: true,
  groups: [
    {
      id: 'g1',
      name: 'heavy',
      kind: 'llm',
      revision: 2,
      member_count: 2,
      enabled_count: 2,
    },
  ],
  nodes: [
    {
      node_id: 'check',
      node_type: 'LLMRequest',
      role: 'checker',
      kind: 'llm',
      model_group_id: 'g1',
      selection_kind: 'group',
      direct_model_id: null,
      direct_harness_profile_id: null,
      direct_provider_connection_id: null,
      candidates: [],
    },
  ],
  visit: {
    current_node_id: 'check',
    current_member_index: 1,
    current_member_id: 'm2',
    current_model_id: 'beta',
    current_profile_or_connection_id: 'c1',
    retries: 0,
    selection_round: 0,
    history: [],
  },
}

const empty: SelectionSummary = {
  group_changes_apply_only_to_new_runs: true,
  groups: [],
  nodes: [],
  visit: null,
}

describe('Selection summary helpers', () => {
  it('detects a meaningful selection summary', () => {
    expect(hasSelectionSummary(summary)).toBe(true)
    expect(hasSelectionSummary(empty)).toBe(false)
    expect(hasSelectionSummary(null)).toBe(false)
  })

  it('extracts current visit target', () => {
    expect(visitCurrentTarget(summary.visit)).toEqual({
      nodeId: 'check',
      memberId: 'm2',
      modelId: 'beta',
    })
    expect(visitCurrentTarget(null)).toEqual({
      nodeId: null,
      memberId: null,
      modelId: null,
    })
  })
})

describe('Selection constants', () => {
  it('exposes candidate state labels', () => {
    expect(CANDIDATE_STATE_LABELS.current).toBe('Текущий')
    expect(CANDIDATE_STATE_LABELS.skipped).toBe('Пропущен')
    expect(CANDIDATE_STATE_LABELS.available).toBe('Ожидает выбора')
    expect(CANDIDATE_STATE_LABELS.consumed).toBe('Использован')
  })

  it('lists model group events', () => {
    expect(MODEL_GROUP_EVENT_TYPES.has('model_group.candidate_selected')).toBe(
      true,
    )
    expect(MODEL_GROUP_EVENT_TYPES.has('model_group.candidate_skipped')).toBe(
      true,
    )
    expect(MODEL_GROUP_EVENT_TYPES.has('model_group.candidate_switched')).toBe(
      true,
    )
    expect(MODEL_GROUP_EVENT_TYPES.has('model_group.exhausted')).toBe(true)
    expect(MODEL_GROUP_EVENT_TYPES.has('run.state_changed')).toBe(false)
  })
})
