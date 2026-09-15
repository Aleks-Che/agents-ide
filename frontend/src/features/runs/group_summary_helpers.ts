import type { SelectionSummary, SelectionVisit } from '../../api/runs'

export function hasSelectionSummary(
  selection: SelectionSummary | null | undefined,
): selection is NonNullable<SelectionSummary> {
  if (!selection) return false
  const nodes = selection.nodes ?? []
  return (
    (selection.groups?.length ?? 0) > 0 ||
    nodes.some((node) => node.selection_kind !== null)
  )
}

export function visitCurrentTarget(visit: SelectionVisit | null | undefined): {
  nodeId: string | null
  memberId: string | null
  modelId: string | null
} {
  if (!visit) {
    return { nodeId: null, memberId: null, modelId: null }
  }
  return {
    nodeId: visit.current_node_id ?? null,
    memberId: visit.current_member_id ?? null,
    modelId: visit.current_model_id ?? null,
  }
}
