import { useId, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import type { AssistanceContext } from '../../api/assistance'

export function AssistanceQuestions({
  context,
  questions,
  loading,
  error,
  hasModel,
  disabled,
  refreshing,
  onChoose,
  onRefresh,
}: {
  context?: AssistanceContext
  questions: string[]
  loading: boolean
  error: Error | null
  hasModel: boolean
  disabled: boolean
  refreshing: boolean
  onChoose: (question: string) => void
  onRefresh: () => void
}) {
  const [expanded, setExpanded] = useState(false)
  const additionalId = useId()
  const problems =
    context?.findings.filter((finding) => finding.attention) ?? []
  const description = problems
    .slice(0, 3)
    .map((finding) => {
      const area =
        !context?.target.chat_id && finding.chat_title
          ? `${finding.chat_title}: `
          : ''
      const explanation = finding.explanation
        .slice(0, 400)
        .trim()
        .replace(/[.!?…]+$/, '')
      const details = [
        finding.node_id ? `этап ${finding.node_id}` : null,
        finding.code,
      ]
        .filter(Boolean)
        .join(', ')
      return `${area}${explanation}.${details ? ` А конкретнее: ${details}.` : ''}`
    })
    .join('; ')
  const primaryQuestion =
    context && problems.length
      ? `Объясни и предложи решение по существующей проблеме: ${description}${problems.length > 3 ? ` Ещё запросов внимания: ${problems.length - 3}.` : ''} Область проекта: ${context.title}.`
      : null
  const canExpand = !loading && !error && questions.length > 0
  const showAdditional = canExpand && expanded

  return (
    <div className="assistance-suggestions" aria-label="Готовые вопросы">
      <div className="assistance-diagnostics-heading">
        <strong>Вопросы по текущей ситуации</strong>
        <button
          className="quiet"
          aria-label="Сгенерировать вопросы заново"
          disabled={!hasModel || loading || refreshing}
          onClick={() => {
            setExpanded(false)
            onRefresh()
          }}
        >
          <RefreshCw size={14} />
        </button>
      </div>
      {primaryQuestion && (
        <button
          className="quiet assistance-primary-question"
          disabled={disabled}
          onClick={() => onChoose(primaryQuestion)}
        >
          {primaryQuestion}
        </button>
      )}
      {hasModel ? (
        <button
          className={`quiet assistance-more-questions${loading ? ' loading' : ''}`}
          aria-label={
            loading
              ? 'Генерируем дополнительные вопросы'
              : showAdditional
                ? 'Скрыть дополнительные вопросы'
                : 'Показать дополнительные вопросы'
          }
          title={
            loading
              ? 'Генерируем дополнительные вопросы'
              : 'Дополнительные вопросы'
          }
          aria-busy={loading}
          aria-expanded={showAdditional}
          aria-controls={additionalId}
          disabled={!canExpand}
          onClick={() => setExpanded(!expanded)}
        >
          <span aria-hidden="true" className="assistance-question-dots">
            <span />
            <span />
            <span />
          </span>
        </button>
      ) : (
        <p className="muted">
          Выберите модель, чтобы сгенерировать дополнительные вопросы.
        </p>
      )}
      <span className="assistance-question-status" role="status">
        {loading
          ? 'Генерируем дополнительные вопросы…'
          : canExpand
            ? 'Дополнительные вопросы готовы.'
            : ''}
      </span>
      {error && (
        <p role="alert" className="error">
          {error.message}
        </p>
      )}
      <div
        id={additionalId}
        className="assistance-additional-questions"
        hidden={!showAdditional}
      >
        {questions.map((question) => (
          <button
            key={question}
            className="quiet"
            disabled={disabled}
            onClick={() => onChoose(question)}
          >
            {question}
          </button>
        ))}
      </div>
    </div>
  )
}
