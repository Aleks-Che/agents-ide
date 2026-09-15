import { formatDateTime, shortHash } from '../../app/format'

export interface BindingSelectionDraft {
  kind: 'direct' | 'group' | null
  model_id?: string
  harness_profile_id?: string
  provider_connection_id?: string
  group_id?: string
}

export interface ModelCandidate {
  id: string
  member_index: number
  enabled: boolean
  harness_profile_id: string | null
  provider_connection_id: string | null
  model_id: string
  params: Record<string, unknown>
}

export interface ModelGroupOption {
  id: string
  name: string
  kind: 'agent' | 'llm'
  members: ModelCandidate[]
  archived: boolean
  revision: number
}

export interface HarnessOption {
  id: string
  name: string
  archived: boolean
}

export interface ConnectionOption {
  id: string
  name: string
  archived: boolean
  manual_models: string[]
  catalog_models: string[]
}

export function describeSelection(
  selection: BindingSelectionDraft | null | undefined,
): string {
  if (!selection || !selection.kind) return 'не задано'
  if (selection.kind === 'group') return `группа ${selection.group_id}`
  return selection.model_id
    ? `модель ${selection.model_id}`
    : 'модель не указана'
}

export function buildDraftFromSelection(
  selection:
    | {
        kind: 'direct'
        model_id: string
        harness_profile_id?: string
        provider_connection_id?: string
      }
    | { kind: 'group'; group_id: string }
    | undefined,
): BindingSelectionDraft {
  if (!selection) return { kind: null }
  if (selection.kind === 'group') {
    return { kind: 'group', group_id: selection.group_id }
  }
  if ('harness_profile_id' in selection && selection.harness_profile_id) {
    return {
      kind: 'direct',
      model_id: selection.model_id,
      harness_profile_id: selection.harness_profile_id,
    }
  }
  return {
    kind: 'direct',
    model_id: selection.model_id,
    provider_connection_id:
      'provider_connection_id' in selection
        ? (selection.provider_connection_id ?? undefined)
        : undefined,
  }
}

export function serialiseSelection(
  draft: BindingSelectionDraft,
  kind: 'agent' | 'llm',
): BindingSelectionDraft | null {
  if (!draft.kind) return null
  if (draft.kind === 'group') {
    if (!draft.group_id) throw new Error('Выберите группу моделей.')
    return { kind: 'group', group_id: draft.group_id }
  }
  if (!draft.model_id?.trim()) throw new Error('Укажите ID модели.')
  if (kind === 'agent') {
    if (!draft.harness_profile_id) throw new Error('Выберите harness-профиль.')
    return {
      kind: 'direct',
      model_id: draft.model_id.trim(),
      harness_profile_id: draft.harness_profile_id,
    }
  }
  if (!draft.provider_connection_id)
    throw new Error('Выберите LLM-подключение.')
  return {
    kind: 'direct',
    model_id: draft.model_id.trim(),
    provider_connection_id: draft.provider_connection_id,
  }
}

export function parseLimitOverrides(text: string): Record<string, number> {
  const parsed: unknown = JSON.parse(text)
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed))
    throw new Error('Лимиты должны быть JSON-объектом.')
  const caps: Record<string, number> = {
    max_calls: 10000,
    max_node_visits: 1000,
    max_backward_transitions: 200,
    max_duration_seconds: 86400,
  }
  for (const [key, value] of Object.entries(parsed)) {
    if (
      !Object.hasOwn(caps, key) ||
      typeof value !== 'number' ||
      !Number.isSafeInteger(value) ||
      value <= 0 ||
      value > caps[key]
    )
      throw new Error(
        `Неверный лимит ${key}. Допустимое целое: 1–${caps[key] ?? 'неизвестно'}.`,
      )
  }
  return parsed as Record<string, number>
}

export function graphRoles(
  graph: Record<string, unknown>,
): Record<string, { kind: 'agent' | 'llm' }> {
  const roles: Record<string, { kind: 'agent' | 'llm' }> = {}
  for (const [role, kind] of Object.entries(
    (graph.roles ?? {}) as Record<string, unknown>,
  )) {
    if (kind === 'agent' || kind === 'llm') roles[role] = { kind }
  }
  for (const node of (graph.nodes ?? []) as Array<{
    type: string
    config?: { role?: string }
  }>) {
    const kind =
      node.type === 'AgentTask'
        ? 'agent'
        : node.type === 'LLMRequest'
          ? 'llm'
          : null
    if (kind && node.config?.role) roles[node.config.role] ??= { kind }
  }
  return roles
}

export function formatWarningCode(code: string): string {
  switch (code) {
    case 'implementer_verifier_same_model':
      return 'Одна модель у реализации и проверки'
    default:
      return code
  }
}

export function formatSettingSource(source: string): string {
  switch (source) {
    case 'binding':
      return 'привязка'
    case 'template':
      return 'версия'
    case 'node':
      return 'узел'
    case 'run':
      return 'запуск'
    case 'default':
      return 'по умолчанию'
    default:
      return source
  }
}

export { formatDateTime, shortHash }
