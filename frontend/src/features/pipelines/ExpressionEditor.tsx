import type { NodeSchemas } from '../../api/graphs'
import { object } from './graph'
import { SchemaField } from './SchemaFields'

function expressionDefault(op: string): Record<string, unknown> {
  if (op === 'const') return { const: true }
  if (op === 'ref') return { ref: '' }
  if (op === 'exists') return { op, target: '' }
  if (op === 'not') return { op, operand: { const: true } }
  if (op === 'all' || op === 'any') return { op, items: [{ const: true }] }
  return { op, left: { ref: '' }, right: { const: true } }
}

export function ExpressionEditor({
  value,
  onChange,
  references,
  ast,
  label = 'Условие',
  depth = 0,
}: {
  value: Record<string, unknown>
  onChange: (value: Record<string, unknown>) => void
  references: string[]
  ast: NodeSchemas['ast']
  label?: string
  depth?: number
}) {
  const op = String(
    value.op ??
      ('const' in value
        ? 'const'
        : 'ref' in value || 'path' in value
          ? 'ref'
          : 'const'),
  )
  if (depth >= ast.max_depth)
    return <p role="alert">Достигнут предел вложенности выражения.</p>
  const child = (field: string, title: string) => (
    <ExpressionEditor
      value={object(value[field])}
      onChange={(next) => onChange({ ...value, [field]: next })}
      references={references}
      ast={ast}
      label={title}
      depth={depth + 1}
    />
  )
  return (
    <fieldset className="expression-editor">
      <legend>{label}</legend>
      <label className="schema-field">
        Оператор
        <select
          value={op}
          onChange={(e) => onChange(expressionDefault(e.target.value))}
        >
          {ast.operators.map((item) => (
            <option key={item}>{item}</option>
          ))}
        </select>
      </label>
      {op === 'const' && (
        <SchemaField
          label="Значение"
          schema={{
            anyOf: ['boolean', 'string', 'number', 'null'].map((type) => ({
              type,
            })),
          }}
          value={'const' in value ? value.const : value.value}
          onChange={(next) => onChange({ const: next })}
        />
      )}
      {(op === 'ref' || op === 'exists') && (
        <SchemaField
          label="Переменная"
          schema={{ type: 'string' }}
          references={references}
          value={op === 'exists' ? value.target : (value.ref ?? value.path)}
          onChange={(next) =>
            onChange(op === 'exists' ? { op, target: next } : { ref: next })
          }
        />
      )}
      {op === 'not' && child('operand', 'Операнд')}
      {['eq', 'ne', 'gt', 'gte', 'lt', 'lte'].includes(op) && (
        <>
          {child('left', 'Левая часть')}
          {child('right', 'Правая часть')}
        </>
      )}
      {(op === 'all' || op === 'any') && (
        <>
          {(Array.isArray(value.items) ? value.items : []).map(
            (item, i, items) => (
              <div key={i}>
                <ExpressionEditor
                  value={object(item)}
                  references={references}
                  ast={ast}
                  label={`Условие ${i + 1}`}
                  depth={depth + 1}
                  onChange={(next) =>
                    onChange({
                      op,
                      items: items.map((old, j) => (i === j ? next : old)),
                    })
                  }
                />
                <button
                  type="button"
                  className="quiet"
                  disabled={items.length <= 1}
                  onClick={() =>
                    onChange({ op, items: items.filter((_, j) => i !== j) })
                  }
                >
                  Удалить условие {i + 1}
                </button>
              </div>
            ),
          )}
          <button
            type="button"
            className="quiet"
            onClick={() =>
              onChange({
                op,
                items: [
                  ...(Array.isArray(value.items) ? value.items : []),
                  { const: true },
                ],
              })
            }
          >
            Добавить условие
          </button>
        </>
      )}
    </fieldset>
  )
}
