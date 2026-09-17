import { useState } from 'react'
import {
  MODEL_PARAM_DESCRIPTORS,
  defaultParamValue,
  descriptorFor,
  formatParamValue,
  parseNumberInput,
  parseStringsInput,
  validateParamValue,
} from './model_params'

export interface ParamDraft {
  text: string
  error: string | null
}

interface MemberParamsEditorProps {
  memberKey: string
  params: Record<string, unknown>
  drafts: Record<string, ParamDraft>
  harnessHint: boolean
  supportedReasoningEfforts?: string[]
  onParams: (next: Record<string, unknown>, removed?: string) => void
  onDraft: (key: string, draft: ParamDraft | null) => void
}

export function MemberParamsEditor({
  memberKey,
  params,
  drafts,
  harnessHint,
  supportedReasoningEfforts,
  onParams,
  onDraft,
}: MemberParamsEditorProps) {
  const [pendingName, setPendingName] = useState('')
  const entries = Object.entries(params).filter(
    ([name]) => name !== 'timeout_seconds',
  )
  const used = new Set(entries.map(([name]) => name))
  const available = MODEL_PARAM_DESCRIPTORS.filter(
    (descriptor) =>
      !used.has(descriptor.name) &&
      (!supportedReasoningEfforts ||
        (descriptor.name === 'reasoning_effort' &&
          supportedReasoningEfforts.length > 0)),
  )
  const selected = available.some(
    (descriptor) => descriptor.name === pendingName,
  )
    ? pendingName
    : (available[0]?.name ?? '')

  function addParam() {
    const descriptor = descriptorFor(selected)
    if (!descriptor) return
    onParams({
      ...params,
      [descriptor.name]:
        descriptor.name === 'reasoning_effort' && supportedReasoningEfforts
          ? supportedReasoningEfforts[0]
          : defaultParamValue(descriptor),
    })
    setPendingName('')
  }

  function removeParam(name: string) {
    const next = { ...params }
    delete next[name]
    onParams(next, name)
  }

  function setParam(name: string, value: unknown) {
    onParams({ ...params, [name]: value })
  }

  return (
    <div className="member-params">
      {entries.map(([name, value]) => {
        const baseDescriptor = descriptorFor(name)
        const descriptor =
          name === 'reasoning_effort' &&
          supportedReasoningEfforts &&
          baseDescriptor
            ? { ...baseDescriptor, options: supportedReasoningEfforts }
            : baseDescriptor
        const draftKey = `${memberKey}:${name}`
        const draft = drafts[draftKey]
        const error =
          draft?.error ??
          (supportedReasoningEfforts &&
          (name !== 'reasoning_effort' ||
            !supportedReasoningEfforts.includes(String(value)))
            ? 'Параметр или значение не поддерживается выбранной моделью.'
            : validateParamValue(name, value))
        const inputId = `param-${draftKey}`
        const hintId = `${inputId}-hint`
        const errorId = `${inputId}-error`
        const inputProps = {
          id: inputId,
          'aria-label': `Значение ${name}`,
          'aria-invalid': Boolean(error),
          'aria-describedby': error ? `${hintId} ${errorId}` : hintId,
        }
        return (
          <div className="param-row" key={name}>
            <label className="param-name" htmlFor={inputId}>
              {descriptor?.title ?? name}
              <br />
              <span className="muted" id={hintId}>
                {descriptor ? descriptor.hint : 'неподдержанный параметр'}
              </span>
            </label>
            {descriptor?.kind === 'boolean' ? (
              <select
                {...inputProps}
                value={String(value)}
                onChange={(event) =>
                  setParam(name, event.target.value === 'true')
                }
              >
                {typeof value !== 'boolean' ? (
                  <option value={String(value)} disabled>
                    {String(value)}
                  </option>
                ) : null}
                <option value="true">true</option>
                <option value="false">false</option>
              </select>
            ) : null}
            {descriptor?.kind === 'enum' ? (
              <select
                {...inputProps}
                value={String(value)}
                onChange={(event) => setParam(name, event.target.value)}
              >
                {!descriptor.options?.includes(value as string) ? (
                  <option value={String(value)} disabled>
                    {String(value)}
                  </option>
                ) : null}
                {descriptor.options?.map((option) => (
                  <option key={option} value={option}>
                    {option}
                  </option>
                ))}
              </select>
            ) : null}
            {descriptor?.kind === 'number' || descriptor?.kind === 'integer' ? (
              <input
                type="text"
                inputMode="decimal"
                spellCheck={false}
                {...inputProps}
                value={draft?.text ?? formatParamValue(value)}
                onChange={(event) => {
                  const parsed = parseNumberInput(
                    descriptor,
                    event.target.value,
                  )
                  onDraft(draftKey, {
                    text: event.target.value,
                    error: parsed.error ?? null,
                  })
                  if (parsed.value !== undefined) {
                    setParam(name, parsed.value)
                  }
                }}
              />
            ) : null}
            {descriptor?.kind === 'strings' ? (
              <textarea
                rows={4}
                spellCheck={false}
                {...inputProps}
                value={draft?.text ?? formatParamValue(value)}
                onChange={(event) => {
                  const parsed = parseStringsInput(
                    descriptor,
                    event.target.value,
                  )
                  onDraft(draftKey, {
                    text: event.target.value,
                    error: parsed.error ?? null,
                  })
                  if (parsed.value !== undefined) {
                    setParam(name, parsed.value)
                  }
                }}
              />
            ) : null}
            {!descriptor ? <span className="muted">—</span> : null}
            <button
              type="button"
              className="quiet danger"
              aria-label={`Удалить параметр ${name}`}
              onClick={() => removeParam(name)}
            >
              ×
            </button>
            {error ? (
              <span className="param-error" role="alert" id={errorId}>
                {error}
              </span>
            ) : null}
          </div>
        )
      })}
      {available.length > 0 ? (
        <div className="param-add">
          <select
            aria-label="Новый параметр"
            value={selected}
            onChange={(event) => setPendingName(event.target.value)}
          >
            {available.map((descriptor) => (
              <option key={descriptor.name} value={descriptor.name}>
                {descriptor.title} ({descriptor.name})
              </option>
            ))}
          </select>
          <button type="button" className="quiet" onClick={addParam}>
            Добавить параметр
          </button>
        </div>
      ) : null}
      {harnessHint ? (
        <p className="hint">
          Для реального запуска параметры должны быть подтверждены свежим
          каталогом выбранной модели. Обновите профиль через проверку
          подключения. Simulated-режим допускает общий набор параметров.
        </p>
      ) : null}
    </div>
  )
}
