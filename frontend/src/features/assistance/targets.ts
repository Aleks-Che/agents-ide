import type { AssistanceTarget } from '../../api/assistance'

// Attach to the actual interactive zone so nested icons select the same target.
export function assistanceZone(target: AssistanceTarget, label: string) {
  return {
    'data-assistance-target': JSON.stringify(target),
    'data-assistance-label': label,
  }
}

export function targetKey(target: AssistanceTarget) {
  return [target.zone, target.project_id ?? '', target.chat_id ?? ''].join(':')
}
