import { ApiError } from '../../api/client'
import { describeError } from '../../api/settings'

export function EditorError({
  error,
  onReload,
}: {
  error: unknown
  onReload: () => void
}) {
  if (!error) return null
  const conflict =
    error instanceof ApiError &&
    ['version_conflict', 'revision_conflict'].includes(error.body.code)
  return (
    <div role="alert" className="error">
      <p>{describeError(error)}</p>
      {conflict ? (
        <>
          <p>
            Правки сохранены в форме. Загрузите текущую версию, чтобы начать
            редактирование заново.
          </p>
          <button type="button" className="quiet" onClick={onReload}>
            Загрузить текущую версию и сбросить правки
          </button>
        </>
      ) : null}
    </div>
  )
}
