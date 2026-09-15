import { ApiError } from '../../api/client'
import type { ApiSchemas } from '../../api/generated'
import { parseLimitOverrides } from '../bindings/utils'

export type StartRequest = ApiSchemas['RunStart']

export function launchParameters(
  executionMode: 'real' | 'simulated',
  inputsText: string,
  limitsText: string,
  commandsText: string,
): ApiSchemas['PreflightRequest'] {
  const inputs: unknown = JSON.parse(inputsText)
  if (!inputs || typeof inputs !== 'object' || Array.isArray(inputs))
    throw new Error('Входы должны быть JSON-объектом.')
  const commandFilter: unknown = JSON.parse(commandsText)
  if (
    commandFilter !== null &&
    (!Array.isArray(commandFilter) ||
      commandFilter.some((item) => typeof item !== 'string' || !item.trim()) ||
      new Set(commandFilter).size !== commandFilter.length)
  )
    throw new Error('Фильтр команд: null или массив уникальных ID команд.')
  return {
    execution_mode: executionMode,
    inputs: inputs as Record<string, unknown>,
    overrides: {
      limit_overrides: parseLimitOverrides(limitsText),
      ...(commandFilter === null
        ? {}
        : { command_filter: commandFilter as string[] }),
    },
  }
}

export function uncertainStart(error: unknown): boolean {
  return (
    !(error instanceof ApiError) ||
    error.status >= 500 ||
    [401, 403, 408, 429].includes(error.status) ||
    ['idempotency_unverifiable', 'idempotency_mismatch'].includes(
      error.body.code,
    )
  )
}

export function launchError(error: unknown): string {
  if (error instanceof ApiError)
    return error.body.message || 'Запрос отклонён. Проверьте входы и параметры.'
  return error instanceof Error ? error.message : 'Не удалось выполнить запрос.'
}

export function pendingStartKey(chatId: string): string {
  return `agents-ide.pending-start.${chatId}`
}

// Stored before POST: closing the dialog or reloading cannot mint another key
// while the outcome of the original request is unknown.
export function loadPendingStart(
  projectId: string,
  chatId: string,
): StartRequest | null {
  const raw = sessionStorage.getItem(pendingStartKey(chatId))
  if (!raw) return null
  const body = JSON.parse(raw) as StartRequest
  if (
    body.project_id !== projectId ||
    body.chat_id !== chatId ||
    !body.idempotency_key
  )
    throw new Error('Сохранённый запрос запуска не соответствует диалогу.')
  return body
}
