import { describe, expect, it } from 'vitest'

import {
  MODEL_PARAM_DESCRIPTORS,
  defaultParamValue,
  descriptorFor,
  formatParamValue,
  parseNumberInput,
  parseStringsInput,
  validateParamValue,
  validateParams,
} from '../src/features/settings/model_params'

describe('validateParamValue', () => {
  it('accepts every default value produced for the supported set', () => {
    for (const descriptor of MODEL_PARAM_DESCRIPTORS) {
      expect(
        validateParamValue(descriptor.name, defaultParamValue(descriptor)),
      ).toBeNull()
    }
  })

  it('rejects unknown parameter names', () => {
    expect(validateParamValue('model_id', 'x')).toContain('Неподдержанный')
    expect(validateParamValue('base_url', 'http://x')).toContain(
      'Неподдержанный',
    )
  })

  it('checks number ranges', () => {
    expect(validateParamValue('temperature', 0)).toBeNull()
    expect(validateParamValue('temperature', 2)).toBeNull()
    expect(validateParamValue('temperature', 0.5)).toBeNull()
    expect(validateParamValue('temperature', 2.5)).toBe('Нужно число от 0 до 2')
    expect(validateParamValue('temperature', -0.1)).toBe(
      'Нужно число от 0 до 2',
    )
    expect(validateParamValue('presence_penalty', -2)).toBeNull()
    expect(validateParamValue('presence_penalty', -2.5)).toBe(
      'Нужно число от -2 до 2',
    )
  })

  it('enforces exclusive minimum for timeout_seconds', () => {
    expect(validateParamValue('timeout_seconds', 0)).toBe(
      'Нужно число больше 0 и не больше 86400',
    )
    expect(validateParamValue('timeout_seconds', 86401)).toBe(
      'Нужно число больше 0 и не больше 86400',
    )
    expect(validateParamValue('timeout_seconds', 0.5)).toBeNull()
    expect(validateParamValue('timeout_seconds', 86400)).toBeNull()
  })

  it('requires integers for integer parameters', () => {
    expect(validateParamValue('max_tokens', 1)).toBeNull()
    expect(validateParamValue('max_tokens', 0)).toBe(
      'Нужно целое число не меньше 1',
    )
    expect(validateParamValue('max_tokens', 1.5)).toBe('Нужно целое число')
    expect(validateParamValue('seed', -5)).toBeNull()
    expect(validateParamValue('seed', 1.5)).toBe('Нужно целое число')
  })

  it('rejects integers the browser cannot preserve exactly', () => {
    for (const name of ['seed', 'max_tokens', 'max_output_tokens']) {
      expect(validateParamValue(name, Number.MAX_SAFE_INTEGER)).toBeNull()
      expect(validateParamValue(name, 9007199254740992)).toContain('точность')
    }
    expect(validateParamValue('seed', Number.MIN_SAFE_INTEGER)).toBeNull()
  })

  it('checks booleans strictly', () => {
    expect(validateParamValue('stream', true)).toBeNull()
    expect(validateParamValue('structured_output', false)).toBeNull()
    expect(validateParamValue('stream', 'true')).toBe(
      'Нужно значение true или false',
    )
    expect(validateParamValue('stream', 1)).toBe(
      'Нужно значение true или false',
    )
  })

  it('checks enum options', () => {
    expect(validateParamValue('reasoning_effort', 'high')).toBeNull()
    expect(validateParamValue('reasoning_effort', 'extreme')).toContain(
      'Допустимые значения',
    )
    expect(validateParamValue('reasoning_effort', 3)).toContain(
      'Допустимые значения',
    )
  })

  it('checks stop sequences list', () => {
    expect(validateParamValue('stop', ['END', ''])).toBeNull()
    expect(validateParamValue('stop', 'END')).toBe('Нужен список строк')
    expect(validateParamValue('stop', ['END', 5])).toBe('Нужен список строк')
    expect(
      validateParamValue(
        'stop',
        Array.from({ length: 17 }, () => 'x'),
      ),
    ).toBe('Не больше 16 значений')
    expect(
      validateParamValue(
        'stop',
        Array.from({ length: 16 }, () => 'x'),
      ),
    ).toBeNull()
  })
})

describe('validateParams', () => {
  it('collects errors per key', () => {
    expect(
      validateParams({ temperature: 1, top_p: 5, unknown_key: 1 }),
    ).toEqual({
      top_p: 'Нужно число от 0 до 1',
      unknown_key: 'Неподдержанный параметр «unknown_key»',
    })
  })

  it('returns empty map for valid params', () => {
    expect(validateParams({ temperature: 0.2, stream: false })).toEqual({})
  })
})

describe('parseNumberInput', () => {
  const temperature = descriptorFor('temperature')!
  const maxTokens = descriptorFor('max_tokens')!

  it('parses valid numbers', () => {
    expect(parseNumberInput(temperature, '0.5')).toEqual({ value: 0.5 })
    expect(parseNumberInput(maxTokens, ' 2048 ')).toEqual({ value: 2048 })
  })

  it('reports empty and non-numeric input', () => {
    expect(parseNumberInput(temperature, '')).toEqual({
      error: 'Введите значение или удалите параметр',
    })
    expect(parseNumberInput(temperature, 'abc')).toEqual({
      error: 'Нужно число',
    })
    expect(parseNumberInput(maxTokens, 'abc')).toEqual({
      error: 'Нужно целое число',
    })
  })

  it('validates parsed value against descriptor bounds', () => {
    expect(parseNumberInput(temperature, '3')).toEqual({
      error: 'Нужно число от 0 до 2',
    })
    expect(parseNumberInput(maxTokens, '2.5')).toEqual({
      error: 'Нужно целое число',
    })
  })

  it('keeps incomplete numeric drafts invalid instead of saving an old value', () => {
    for (const text of ['-', '.', '1e', '1e-', '0x10', 'Infinity', 'NaN']) {
      expect(parseNumberInput(temperature, text).error).toBeTruthy()
    }
    expect(parseNumberInput(temperature, '1e-2')).toEqual({ value: 0.01 })
    expect(
      parseNumberInput(descriptorFor('seed')!, '9007199254740993').error,
    ).toContain('точность')
  })
})

describe('parseStringsInput', () => {
  const stop = descriptorFor('stop')!

  it('round-trips exact sequences including line endings and empty strings', () => {
    const sequences = ['\n\n', 'END\r\n', '', ' STOP ', '\t"\\', 'конец']
    expect(parseStringsInput(stop, formatParamValue(sequences))).toEqual({
      value: sequences,
    })
  })

  it('supports empty lists and empty sequences without conflating them', () => {
    for (const value of [[], [''], ['', '']]) {
      expect(parseStringsInput(stop, formatParamValue(value))).toEqual({
        value,
      })
    }
  })

  it('rejects malformed JSON and wrong element types', () => {
    for (const text of ['', '\n\n', '[', '["END",]', 'END\nSTOP']) {
      expect(parseStringsInput(stop, text).error).toContain('JSON')
    }
    for (const value of [null, true, 1, {}, 'END', ['END', 5]]) {
      expect(parseStringsInput(stop, JSON.stringify(value)).error).toBe(
        'Нужен список строк',
      )
    }
  })

  it('enforces the item limit', () => {
    const text = JSON.stringify(Array.from({ length: 17 }, () => 'x'))
    expect(parseStringsInput(stop, text)).toEqual({
      error: 'Не больше 16 значений',
    })
  })
})

describe('formatParamValue', () => {
  it('round-trips lists and scalars to editable text', () => {
    expect(JSON.parse(formatParamValue(['a', 'b']))).toEqual(['a', 'b'])
    expect(formatParamValue(0.5)).toBe('0.5')
    expect(formatParamValue(true)).toBe('true')
  })
})
