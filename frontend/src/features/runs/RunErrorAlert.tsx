import { useId, useState } from 'react'
import type { PreflightIssue } from '../../api/bindings'
import { ApiError } from '../../api/client'
import { describeRunError } from '../../api/runs'
import { Modal } from '../../app/Modal'

export function RunErrorAlert({ error }: { error: unknown }) {
  const [detailsOpen, setDetailsOpen] = useState(false)
  const titleId = useId()
  if (
    !(error instanceof ApiError) ||
    error.body.code !== 'graph_validation_failed'
  )
    return (
      <p className="error" role="alert">
        {describeRunError(error)}
      </p>
    )

  const issues: PreflightIssue[] = Array.isArray(error.body.details.errors)
    ? error.body.details.errors
    : []
  return (
    <>
      <p className="error" role="alert">
        Граф не прошёл{' '}
        <button
          type="button"
          className="preflight-error-link"
          aria-label="Открыть ошибки preflight"
          aria-haspopup="dialog"
          onClick={() => setDetailsOpen(true)}
        >
          preflight
        </button>
      </p>
      {detailsOpen ? (
        <Modal onClose={() => setDetailsOpen(false)} labelledBy={titleId}>
          <div className="dialog wide">
            <header>
              <h3 id={titleId}>Проверка перед запуском (preflight)</h3>
              <button
                type="button"
                className="quiet"
                aria-label="Закрыть"
                onClick={() => setDetailsOpen(false)}
              >
                ×
              </button>
            </header>
            <p>
              Preflight проверяет граф, входные данные и настройки исполнителей
              перед запуском. Ниже — причины, по которым эта попытка запуска
              отклонена. Исправьте указанные ошибки и повторите запуск.
            </p>
            {issues.length ? (
              <ul className="errors" aria-label="Ошибки preflight">
                {issues.map((issue, index) => (
                  <li key={`${issue.code}-${index}`}>
                    {issue.node_id ? (
                      <strong>Узел {issue.node_id}: </strong>
                    ) : null}
                    {issue.message} <code>({issue.code})</code>
                    {issue.details && Object.keys(issue.details).length ? (
                      <details>
                        <summary tabIndex={0}>Подробности ошибки</summary>
                        <pre>{JSON.stringify(issue.details, null, 2)}</pre>
                      </details>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : (
              <p>Сервер не передал подробности проверки.</p>
            )}
          </div>
        </Modal>
      ) : null}
    </>
  )
}
