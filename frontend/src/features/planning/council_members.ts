import type { PlanningMemberSpec } from '../../api/planning'

export const blankMember = (
  role: 'participant' | 'merger',
): PlanningMemberSpec => ({
  role,
  selection: { kind: 'direct', model_id: '', provider_connection_id: '' },
})

export const isAgentSelection = (
  selection: PlanningMemberSpec['selection'],
): selection is {
  kind: 'direct'
  model_id: string
  harness_profile_id: string
} => 'harness_profile_id' in selection

export const isLLMSelection = (
  selection: PlanningMemberSpec['selection'],
): selection is {
  kind: 'direct'
  model_id: string
  provider_connection_id: string
} => 'provider_connection_id' in selection

export const validCouncilMember = (spec: PlanningMemberSpec) =>
  spec.selection.kind === 'group'
    ? Boolean(spec.selection.group_id)
    : Boolean(
        spec.selection.model_id.trim() &&
        (isAgentSelection(spec.selection)
          ? spec.selection.harness_profile_id
          : isLLMSelection(spec.selection)
            ? spec.selection.provider_connection_id
            : ''),
      )
