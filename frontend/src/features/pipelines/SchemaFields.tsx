import { useId, useState } from 'react'
import type { Schema } from '../../api/graphs'
import { defaultValue, object } from './graph'

const names: Record<string, string> = {
  prompt: 'Промпт',
  prompt_repair: 'Промпт исправления',
  prompt_next_item: 'Промпт следующего пункта',
  commands: 'Команды',
  sources: 'Источники',
  operation: 'Операция',
  program: 'Программа',
  args: 'Аргументы',
  cwd: 'Рабочий каталог',
  success_exit_codes: 'Успешные коды выхода',
  required: 'Обязательный',
  timeout_seconds: 'Тайм-аут (секунды)',
  max_retries: 'Повторы',
  response_format: 'Формат ответа',
  output_schema: 'Схема результата',
  path: 'Путь',
  kind: 'Вид',
  role: 'Роль',
  params: 'Параметры модели',
  plan_id: 'ID плана',
  verification_node_id: 'Узел проверки',
  commit_node_id: 'Узел коммита',
  initial_mode: 'Начальный режим',
  completion_policy: 'Условие завершения',
  scope: 'Область',
  message: 'Сообщение коммита',
  allowlist: 'Разрешённые пути',
  label: 'Подпись',
  context_paths: 'Пути контекста',
  ref: 'Переменная контекста',
  expression: 'Условие',
}
const fieldName = (name: string) => names[name] ?? name

export function PromptField({
  label,
  value,
  onChange,
  references,
  maxLength,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  references: string[]
  maxLength?: number
}) {
  const id = useId()
  return (
    <div className="schema-field">
      <label htmlFor={id}>{label}</label>
      <textarea
        id={id}
        rows={6}
        value={value}
        maxLength={maxLength}
        onChange={(e) => onChange(e.target.value)}
      />
      <select
        aria-label={`Вставить переменную: ${label}`}
        value=""
        onChange={(e) => {
          if (e.target.value) onChange(`${value}{{ ${e.target.value} }}`)
        }}
      >
        <option value="">Вставить переменную…</option>
        {references.map((ref) => (
          <option key={ref}>{ref}</option>
        ))}
      </select>
      <small className="muted">
        Подстановка {'{{ input.name }}'} выполняется один раз; доступ проверяет
        сервер.
      </small>
    </div>
  )
}

function matches(schema: Schema, value: unknown): boolean {
  if ('const' in schema) return value === schema.const
  if (schema.enum) return schema.enum.includes(value)
  if (schema.type === 'array') return Array.isArray(value)
  if (schema.type === 'object')
    return value !== null && typeof value === 'object' && !Array.isArray(value)
  if (schema.type === 'null') return value === null
  if (schema.type === 'integer')
    return typeof value === 'number' && Number.isInteger(value)
  return typeof value === schema.type
}

export function SchemaField({
  schema,
  value,
  onChange,
  label,
  references = [],
  depth = 0,
}: {
  schema: Schema
  value: unknown
  onChange: (value: unknown) => void
  label: string
  references?: string[]
  depth?: number
}) {
  const id = useId()
  if (depth > 16) return <p role="alert">Слишком глубокая структура: {label}</p>
  const variants =
    schema.oneOf ??
    schema.anyOf ??
    (Array.isArray(schema.type)
      ? schema.type.map((type) => ({ ...schema, type }))
      : null)
  if (variants) {
    const index = Math.max(
      0,
      variants.findIndex((s) => matches(s, value)),
    )
    return (
      <fieldset className="schema-group">
        <legend>{label}</legend>
        <select
          aria-label={`Способ: ${label}`}
          value={index}
          onChange={(e) =>
            onChange(defaultValue(variants[Number(e.target.value)]))
          }
        >
          {variants.map((variant, i) => (
            <option key={i} value={i}>
              {variant.title ??
                (variant.properties?.ref
                  ? 'Из контекста'
                  : variant.type === 'array'
                    ? 'Список'
                    : String(variant.type ?? i + 1))}
            </option>
          ))}
        </select>
        <SchemaField
          schema={variants[index]}
          value={value}
          onChange={onChange}
          label={label}
          references={references}
          depth={depth + 1}
        />
      </fieldset>
    )
  }
  if (schema.const !== undefined)
    return (
      <p>
        {label}: {String(schema.const)}
      </p>
    )
  if (schema.enum)
    return (
      <label className="schema-field">
        {label}
        <select
          value={JSON.stringify(value)}
          onChange={(e) => onChange(JSON.parse(e.target.value))}
        >
          {!schema.enum.includes(value) && (
            <option value={JSON.stringify(value)}>Выберите…</option>
          )}
          {schema.enum.map((item, i) => (
            <option key={i} value={JSON.stringify(item)}>
              {String(item)}
            </option>
          ))}
        </select>
      </label>
    )
  if (schema.type === 'array') {
    const items = Array.isArray(value) ? value : []
    return (
      <fieldset className="schema-group">
        <legend>{label}</legend>
        {items.map((item, index) => (
          <div key={index} className="schema-array-item">
            <SchemaField
              schema={schema.items ?? {}}
              value={item}
              label={`${label} ${index + 1}`}
              references={references}
              depth={depth + 1}
              onChange={(next) =>
                onChange(items.map((old, i) => (i === index ? next : old)))
              }
            />
            <div className="editor-actions">
              <button
                type="button"
                className="quiet"
                aria-label={`Выше: ${label} ${index + 1}`}
                disabled={index === 0}
                onClick={() => {
                  const next = [...items]
                  ;[next[index - 1], next[index]] = [
                    next[index],
                    next[index - 1],
                  ]
                  onChange(next)
                }}
              >
                ↑
              </button>
              <button
                type="button"
                className="quiet"
                onClick={() => onChange(items.filter((_, i) => i !== index))}
              >
                Удалить: {label} {index + 1}
              </button>
            </div>
          </div>
        ))}
        <button
          type="button"
          className="quiet"
          disabled={items.length >= (schema.maxItems ?? 200)}
          onClick={() => onChange([...items, defaultValue(schema.items ?? {})])}
        >
          Добавить: {label}
        </button>
      </fieldset>
    )
  }
  if (schema.type === 'object' || schema.properties)
    return (
      <ObjectFields
        schema={schema}
        value={object(value)}
        onChange={onChange}
        label={label}
        references={references}
        depth={depth + 1}
      />
    )
  if (!schema.type)
    return (
      <ValueField
        label={label}
        value={value}
        onChange={onChange}
        references={references}
        depth={depth + 1}
      />
    )
  if (schema.type === 'null') return <p>{label}: null</p>
  if (schema.type === 'boolean')
    return (
      <label className="checkbox-row">
        <input
          type="checkbox"
          checked={value === true}
          onChange={(e) => onChange(e.target.checked)}
        />
        {label}
      </label>
    )
  if (schema.type === 'number' || schema.type === 'integer')
    return (
      <label className="schema-field">
        {label}
        <input
          type="number"
          value={typeof value === 'number' ? value : ''}
          min={schema.minimum}
          max={schema.maximum}
          step={schema.type === 'integer' ? 1 : 'any'}
          onChange={(e) =>
            onChange(e.target.value === '' ? null : Number(e.target.value))
          }
        />
      </label>
    )
  if (/Промпт|prompt/.test(label))
    return (
      <PromptField
        label={label}
        value={String(value ?? '')}
        onChange={onChange}
        references={references}
        maxLength={schema.maxLength}
      />
    )
  return (
    <div className="schema-field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        value={String(value ?? '')}
        minLength={schema.minLength}
        maxLength={schema.maxLength}
        pattern={schema.pattern}
        list={references.length ? `${id}-refs` : undefined}
        onChange={(e) => onChange(e.target.value)}
      />
      {references.length > 0 && (
        <datalist id={`${id}-refs`}>
          {references.map((ref) => (
            <option key={ref} value={ref} />
          ))}
        </datalist>
      )}
      {schema.description && (
        <small className="muted">{schema.description}</small>
      )}
    </div>
  )
}

export function ObjectFields({
  schema,
  value,
  onChange,
  label,
  references = [],
  depth = 0,
  exclude = [],
}: {
  schema: Schema
  value: Record<string, unknown>
  onChange: (value: Record<string, unknown>) => void
  label: string
  references?: string[]
  depth?: number
  exclude?: string[]
}) {
  const [newKey, setNewKey] = useState('')
  const properties = schema.properties ?? {}
  const keys = [
    ...new Set([...Object.keys(properties), ...Object.keys(value)]),
  ].filter((key) => !exclude.includes(key))
  const additional = schema.additionalProperties !== false
  return (
    <fieldset className="schema-group">
      <legend>{label}</legend>
      {keys.map((key) => {
        const field =
          properties[key] ??
          (typeof schema.additionalProperties === 'object'
            ? schema.additionalProperties
            : {})
        const required = schema.required?.includes(key)
        const present = value[key] !== undefined
        return (
          <div className="schema-property" key={key}>
            {!required && (
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={present}
                  aria-label={`Задать: ${fieldName(key)}`}
                  onChange={(e) => {
                    const next = { ...value }
                    if (e.target.checked) next[key] = defaultValue(field)
                    else delete next[key]
                    onChange(next)
                  }}
                />
                {fieldName(key)}
              </label>
            )}
            {(present || required) && (
              <SchemaField
                schema={field}
                value={value[key]}
                onChange={(next) => onChange({ ...value, [key]: next })}
                label={fieldName(key)}
                references={references}
                depth={depth}
              />
            )}
          </div>
        )
      })}
      {additional && (
        <div className="editor-actions">
          <input
            aria-label={`Новое поле: ${label}`}
            placeholder="Имя поля"
            value={newKey}
            onChange={(e) => setNewKey(e.target.value)}
          />
          <button
            type="button"
            className="quiet"
            disabled={!newKey.trim() || Object.hasOwn(value, newKey.trim())}
            onClick={() => {
              onChange({
                ...value,
                [newKey.trim()]: defaultValue(
                  typeof schema.additionalProperties === 'object'
                    ? schema.additionalProperties
                    : { type: 'string' },
                ),
              })
              setNewKey('')
            }}
          >
            Добавить поле
          </button>
        </div>
      )}
    </fieldset>
  )
}

function ValueField({
  label,
  value,
  onChange,
  references,
  depth,
}: {
  label: string
  value: unknown
  onChange: (value: unknown) => void
  references: string[]
  depth: number
}) {
  const type =
    value === null
      ? 'null'
      : Array.isArray(value)
        ? 'array'
        : typeof value === 'undefined'
          ? 'string'
          : typeof value
  return (
    <div>
      <label className="schema-field">
        Тип: {label}
        <select
          value={type}
          onChange={(e) => onChange(defaultValue({ type: e.target.value }))}
        >
          {['string', 'number', 'boolean', 'null', 'array', 'object'].map(
            (t) => (
              <option key={t}>{t}</option>
            ),
          )}
        </select>
      </label>
      <SchemaField
        schema={{ type }}
        value={value}
        onChange={onChange}
        label={label}
        references={references}
        depth={depth}
      />
    </div>
  )
}

// Edits JSON Schema itself, while retaining constraints not changed by the user.
export function DataSchemaEditor({
  schema,
  onChange,
  label,
  depth = 0,
}: {
  schema: Schema
  onChange: (schema: Schema) => void
  label: string
  depth?: number
}) {
  const [newKey, setNewKey] = useState('')
  if (depth > 12) return <p>Вложенность схемы превышает 12.</p>
  const type =
    typeof schema.type === 'string'
      ? schema.type
      : schema.enum?.length
        ? schema.enum[0] === null
          ? 'null'
          : typeof schema.enum[0]
        : schema.items
          ? 'array'
          : 'object'
  return (
    <fieldset className="schema-group">
      <legend>{label}</legend>
      <label className="schema-field">
        Тип данных
        <select
          value={type}
          onChange={(e) => {
            const next = { ...schema, type: e.target.value }
            delete next.properties
            delete next.required
            delete next.items
            delete next.additionalProperties
            delete next.enum
            delete next.const
            if (e.target.value === 'object') {
              next.properties = {}
              next.additionalProperties = false
            }
            if (e.target.value === 'array') next.items = { type: 'string' }
            onChange(next)
          }}
        >
          {[
            'object',
            'array',
            'string',
            'number',
            'integer',
            'boolean',
            'null',
          ].map((t) => (
            <option key={t}>{t}</option>
          ))}
        </select>
      </label>
      {type === 'object' && (
        <>
          {Object.entries(schema.properties ?? {}).map(([key, child]) => (
            <div className="schema-array-item" key={key}>
              <DataSchemaEditor
                schema={child}
                label={key}
                depth={depth + 1}
                onChange={(next) =>
                  onChange({
                    ...schema,
                    properties: { ...schema.properties, [key]: next },
                  })
                }
              />
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={schema.required?.includes(key) ?? false}
                  onChange={(e) =>
                    onChange({
                      ...schema,
                      required: e.target.checked
                        ? [...(schema.required ?? []), key]
                        : (schema.required ?? []).filter((k) => k !== key),
                    })
                  }
                />
                Обязательное поле: {key}
              </label>
              <button
                type="button"
                className="quiet"
                onClick={() => {
                  const props = { ...schema.properties }
                  delete props[key]
                  onChange({
                    ...schema,
                    properties: props,
                    required: (schema.required ?? []).filter((k) => k !== key),
                  })
                }}
              >
                Удалить поле: {key}
              </button>
            </div>
          ))}
          <div className="editor-actions">
            <input
              aria-label={`Имя нового поля: ${label}`}
              value={newKey}
              onChange={(e) => setNewKey(e.target.value)}
            />
            <button
              type="button"
              className="quiet"
              disabled={
                !/^[a-zA-Z][a-zA-Z0-9_-]*$/.test(newKey) ||
                Object.hasOwn(schema.properties ?? {}, newKey)
              }
              onClick={() => {
                onChange({
                  ...schema,
                  type: 'object',
                  properties: {
                    ...schema.properties,
                    [newKey]: { type: 'string' },
                  },
                })
                setNewKey('')
              }}
            >
              Добавить поле схемы
            </button>
          </div>
        </>
      )}
      {type === 'array' && (
        <DataSchemaEditor
          label="Элемент списка"
          schema={schema.items ?? { type: 'string' }}
          onChange={(items) => onChange({ ...schema, items })}
          depth={depth + 1}
        />
      )}
      <ObjectFields
        label="Ограничения схемы"
        schema={{
          type: 'object',
          additionalProperties: false,
          properties: {
            ...(type === 'object'
              ? { additionalProperties: { type: 'boolean' } }
              : {}),
            ...(type === 'string'
              ? {
                  minLength: { type: 'integer', minimum: 0 },
                  maxLength: { type: 'integer', minimum: 0 },
                  pattern: { type: 'string' },
                }
              : {}),
            ...(['number', 'integer'].includes(type)
              ? { minimum: { type: 'number' }, maximum: { type: 'number' } }
              : {}),
            ...(type === 'array'
              ? {
                  minItems: { type: 'integer', minimum: 0 },
                  maxItems: { type: 'integer', minimum: 0 },
                }
              : {}),
            ...(['string', 'number', 'integer', 'boolean', 'null'].includes(
              type,
            )
              ? { enum: { type: 'array', items: { type }, minItems: 1 } }
              : {}),
          },
        }}
        value={schema}
        exclude={Object.keys(schema).filter(
          (key) =>
            ![
              'additionalProperties',
              'minLength',
              'maxLength',
              'pattern',
              'minimum',
              'maximum',
              'minItems',
              'maxItems',
              'enum',
            ].includes(key),
        )}
        onChange={(next) => onChange(next as Schema)}
      />
      <label className="schema-field">
        Описание
        <input
          value={schema.description ?? ''}
          onChange={(e) => onChange({ ...schema, description: e.target.value })}
        />
      </label>
    </fieldset>
  )
}
