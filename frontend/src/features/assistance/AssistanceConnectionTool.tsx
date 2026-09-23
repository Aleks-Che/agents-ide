import { useRef, useState } from 'react'
import {
  assistanceApi,
  type CommitConnectionReview,
} from '../../api/assistance'
import type { AssistanceToolProps } from './AssistanceGitTool'

const fields: Record<string, string> = {
  base_url: 'адрес сервера',
  protocol: 'протокол',
  provider_kind: 'тип подключения',
  secret_reference: 'ссылка на ключ',
}

export function AssistanceConnectionTool({
  tool,
  target,
  csrf,
  disabled,
  onBusy,
  onResult,
}: AssistanceToolProps) {
  const [opened, setOpened] = useState(false)
  const [review, setReview] = useState<CommitConnectionReview>()
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [acknowledged, setAcknowledged] = useState(false)
  const [error, setError] = useState<string>()
  const pending = useRef(false)

  async function load() {
    setOpened(true)
    setLoading(true)
    setAcknowledged(false)
    setReview(undefined)
    setError(undefined)
    try {
      setReview(await assistanceApi.reviewCommitConnection(tool.run_id))
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : 'Не удалось сравнить подключение.',
      )
    } finally {
      setLoading(false)
    }
  }

  async function execute() {
    if (
      !review?.can_accept ||
      !acknowledged ||
      !csrf ||
      disabled ||
      pending.current
    )
      return
    pending.current = true
    setBusy(true)
    setError(undefined)
    onBusy(true)
    try {
      const result = await assistanceApi.executeTool(
        {
          tool: tool.name,
          target,
          run_id: tool.run_id,
          comparison_id: review.comparison_id,
          expected_state_version: review.state_version,
          acknowledge_risk: true,
          confirm_resume: true,
        },
        csrf,
      )
      setOpened(false)
      onResult(result)
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : 'Не удалось обновить подключение.',
      )
    } finally {
      pending.current = false
      setBusy(false)
      onBusy(false)
    }
  }

  return (
    <section
      className="assistance-tool"
      aria-label={`Инструмент помощника: ${tool.title}`}
    >
      <strong>Решение для «{tool.title}»</strong>
      <p>{tool.description}</p>
      {!opened && (
        <button
          type="button"
          disabled={disabled || !csrf}
          onClick={() => void load()}
        >
          Решить через помощника
        </button>
      )}
      {opened && (
        <form
          className="git-changes-review"
          aria-label="Обновление подключения GitCommit"
          onSubmit={(event) => {
            event.preventDefault()
            void execute()
          }}
        >
          <p role="status">
            Ожидается ваше подтверждение. Изучите сравнение и отметьте согласие,
            затем нажмите «Обновить версию подключения и продолжить».
          </p>
          {loading && <p role="status">Сравниваем подключение…</p>}
          {review && (
            <>
              <p>
                Подключение:{' '}
                <strong>{review.connection_name ?? 'недоступно'}</strong>.
                Модель: {review.model ?? 'не задана'}.
              </p>
              <p>
                Ожидаемая версия: {review.expected_version ?? 'неизвестна'}.
                Текущая версия: {review.current_version ?? 'неизвестна'}.
              </p>
              <p>{review.assessment}</p>
              {review.changed_fields.length > 0 && (
                <p>
                  Различаются:{' '}
                  {review.changed_fields
                    .map((field) => fields[field] ?? field)
                    .join(', ')}
                  .
                </p>
              )}
              <p>{review.effect}</p>
              {review.blockers.map((blocker) => (
                <p className="error" key={blocker}>
                  {blocker}
                </p>
              ))}
              <label>
                <input
                  type="checkbox"
                  checked={acknowledged}
                  disabled={busy || disabled || !review.can_accept}
                  onChange={(event) => setAcknowledged(event.target.checked)}
                />
                Я изучил сравнение и подтверждаю обновление версии и продолжение
                GitCommit.
              </label>
              <button
                type="submit"
                disabled={
                  busy ||
                  disabled ||
                  !csrf ||
                  !acknowledged ||
                  !review.can_accept
                }
              >
                {tool.confirmation_label}
              </button>
            </>
          )}
          <button
            type="button"
            disabled={loading || busy || disabled}
            onClick={() => void load()}
          >
            Обновить сравнение
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => setOpened(false)}
          >
            Закрыть
          </button>
        </form>
      )}
      {busy && <p role="status">Проверяем подключение и продолжаем запуск…</p>}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </section>
  )
}
