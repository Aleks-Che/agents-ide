/**
 * Общий допустимый набор параметров. Список и границы повторяют серверный
 * PARAM_SCHEMA (backend/src/agents_ide/domain/graph_validation.py); сервер
 * остаётся окончательной проверкой. Поддержка параметра конкретной моделью
 * пока не подтверждена (capability = unverified), поэтому редактор знает
 * только общий допустимый набор. Целые числа дополнительно ограничены точным
 * диапазоном JavaScript; сервер проверяет PARAM_SCHEMA перед запуском.
 */

export type ParamKind = 'number' | 'integer' | 'boolean' | 'enum' | 'strings'

export interface ParamDescriptor {
  name: string
  kind: ParamKind
  title: string
  hint: string
  min?: number
  max?: number
  exclusiveMin?: boolean
  options?: readonly string[]
  maxItems?: number
}

export const REASONING_EFFORT_OPTIONS = [
  'none',
  'minimal',
  'low',
  'medium',
  'high',
  'xhigh',
  'max',
  'ultra',
] as const

export const MODEL_PARAM_DESCRIPTORS: readonly ParamDescriptor[] = [
  {
    name: 'reasoning_effort',
    kind: 'enum',
    title: 'Усилие рассуждения',
    hint: `одно из: ${REASONING_EFFORT_OPTIONS.join(', ')}`,
    options: REASONING_EFFORT_OPTIONS,
  },
  {
    name: 'temperature',
    kind: 'number',
    title: 'Температура',
    hint: 'число от 0 до 2',
    min: 0,
    max: 2,
  },
  {
    name: 'top_p',
    kind: 'number',
    title: 'Top-P',
    hint: 'число от 0 до 1',
    min: 0,
    max: 1,
  },
  {
    name: 'max_tokens',
    kind: 'integer',
    title: 'Максимум токенов',
    hint: 'целое число не меньше 1',
    min: 1,
  },
  {
    name: 'max_output_tokens',
    kind: 'integer',
    title: 'Максимум выходных токенов',
    hint: 'целое число не меньше 1',
    min: 1,
  },
  {
    name: 'seed',
    kind: 'integer',
    title: 'Seed',
    hint: 'целое число',
  },
  {
    name: 'stop',
    kind: 'strings',
    title: 'Стоп-последовательности',
    hint: 'JSON-список до 16 строк, например ["END", "\\n\\n"]. [] — пустой список',
    maxItems: 16,
  },
  {
    name: 'frequency_penalty',
    kind: 'number',
    title: 'Штраф частоты',
    hint: 'число от −2 до 2',
    min: -2,
    max: 2,
  },
  {
    name: 'presence_penalty',
    kind: 'number',
    title: 'Штраф присутствия',
    hint: 'число от −2 до 2',
    min: -2,
    max: 2,
  },
  {
    name: 'stream',
    kind: 'boolean',
    title: 'Потоковая выдача',
    hint: 'true или false',
  },
  {
    name: 'structured_output',
    kind: 'boolean',
    title: 'Структурированный ответ',
    hint: 'true или false',
  },
  {
    name: 'timeout_seconds',
    kind: 'number',
    title: 'Тайм-аут, с',
    hint: 'число больше 0 и не больше 86400',
    min: 0,
    max: 86400,
    exclusiveMin: true,
  },
]

export function descriptorFor(name: string): ParamDescriptor | undefined {
  return MODEL_PARAM_DESCRIPTORS.find((descriptor) => descriptor.name === name)
}

function rangeError(descriptor: ParamDescriptor): string {
  const unit = descriptor.kind === 'integer' ? 'целое число' : 'число'
  const { min, max, exclusiveMin } = descriptor
  if (min !== undefined && max !== undefined) {
    return exclusiveMin
      ? `Нужно ${unit} больше ${min} и не больше ${max}`
      : `Нужно ${unit} от ${min} до ${max}`
  }
  if (min !== undefined) {
    return exclusiveMin
      ? `Нужно ${unit} больше ${min}`
      : `Нужно ${unit} не меньше ${min}`
  }
  if (max !== undefined) return `Нужно ${unit} не больше ${max}`
  return `Нужно ${unit}`
}

export function validateParamValue(
  name: string,
  value: unknown,
): string | null {
  const descriptor = descriptorFor(name)
  if (!descriptor) return `Неподдержанный параметр «${name}»`
  switch (descriptor.kind) {
    case 'boolean':
      return typeof value === 'boolean' ? null : 'Нужно значение true или false'
    case 'enum':
      return typeof value === 'string' && descriptor.options?.includes(value)
        ? null
        : `Допустимые значения: ${descriptor.options?.join(', ')}`
    case 'strings': {
      if (
        !Array.isArray(value) ||
        value.some((item) => typeof item !== 'string')
      ) {
        return 'Нужен список строк'
      }
      if (
        descriptor.maxItems !== undefined &&
        value.length > descriptor.maxItems
      ) {
        return `Не больше ${descriptor.maxItems} значений`
      }
      return null
    }
    case 'number':
    case 'integer': {
      if (typeof value !== 'number' || !Number.isFinite(value)) {
        return descriptor.kind === 'integer'
          ? 'Нужно целое число'
          : 'Нужно число'
      }
      if (descriptor.kind === 'integer' && !Number.isInteger(value)) {
        return 'Нужно целое число'
      }
      if (descriptor.kind === 'integer' && !Number.isSafeInteger(value)) {
        return 'Целое число должно быть от −9007199254740991 до 9007199254740991: иначе браузер теряет точность'
      }
      if (
        descriptor.min !== undefined &&
        (descriptor.exclusiveMin
          ? value <= descriptor.min
          : value < descriptor.min)
      ) {
        return rangeError(descriptor)
      }
      if (descriptor.max !== undefined && value > descriptor.max) {
        return rangeError(descriptor)
      }
      return null
    }
  }
}

export function validateParams(
  params: Record<string, unknown>,
): Record<string, string> {
  const errors: Record<string, string> = {}
  for (const [name, value] of Object.entries(params)) {
    const error = validateParamValue(name, value)
    if (error) errors[name] = error
  }
  return errors
}

export interface ParsedParam {
  value?: unknown
  error?: string
}

export function parseNumberInput(
  descriptor: ParamDescriptor,
  text: string,
): ParsedParam {
  const trimmed = text.trim()
  if (!trimmed) return { error: 'Введите значение или удалите параметр' }
  const value = Number(trimmed)
  if (
    !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(trimmed) ||
    !Number.isFinite(value)
  ) {
    return {
      error:
        descriptor.kind === 'integer' ? 'Нужно целое число' : 'Нужно число',
    }
  }
  const error = validateParamValue(descriptor.name, value)
  return error ? { error } : { value }
}

export function parseStringsInput(
  descriptor: ParamDescriptor,
  text: string,
): ParsedParam {
  let value: unknown
  try {
    value = JSON.parse(text)
  } catch {
    return { error: 'Введите JSON-список строк, например ["END", "\\n\\n"]' }
  }
  const error = validateParamValue(descriptor.name, value)
  return error ? { error } : { value }
}

export function formatParamValue(value: unknown): string {
  if (Array.isArray(value)) return JSON.stringify(value, null, 2)
  if (typeof value === 'string') return value
  return String(value)
}

export function defaultParamValue(descriptor: ParamDescriptor): unknown {
  switch (descriptor.kind) {
    case 'boolean':
      return true
    case 'enum':
      return descriptor.options?.[0] ?? ''
    case 'strings':
      return []
    case 'integer':
      return descriptor.min ?? 0
    case 'number':
      if (descriptor.exclusiveMin) return (descriptor.min ?? 0) + 1
      return descriptor.min ?? 0
  }
}
