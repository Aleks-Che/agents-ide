import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { runsApi, type GitChangesReview, type RunRecord } from '../../api/runs'
import { GitHeadComparison } from './GitHeadComparison'
import './git-changes.css'

const risks = {
  low: 'Низкий',
  medium: 'Средний',
  high: 'Высокий',
  unknown: 'Не установлен',
}
const changes = { added: 'Добавлен', modified: 'Изменён', removed: 'Удалён' }
const fields: Record<string, string> = {
  sha256: 'содержимое',
  size: 'размер',
  mode: 'права',
  ignored: 'игнорирование Git',
  missing: 'наличие',
}

export type GitChangesResolutionPayload = {
  git_changes: {
    comparison_id: string
    paths: string[]
    accept_head: boolean
    acknowledge_risk: true
  }
}

export function GitChangesResolution({
  run,
  onSubmit,
  onClose,
  busy = false,
  resumeAfterAcceptance = false,
}: {
  run: RunRecord
  onSubmit: (
    payload: GitChangesResolutionPayload,
    review: GitChangesReview,
  ) => void
  onClose: () => void
  busy?: boolean
  resumeAfterAcceptance?: boolean
}) {
  const form = useRef<HTMLFormElement>(null)
  useEffect(() => {
    const element = form.current
    const container = element?.closest<HTMLElement>('[data-assistance-scroll]')
    if (element && container) {
      // Keep popup chrome outside the scroll operation, even for a tall review.
      container.scrollTo({
        top:
          container.scrollTop +
          element.getBoundingClientRect().top -
          container.getBoundingClientRect().top -
          8,
      })
    } else {
      element?.scrollIntoView({ block: 'start' })
    }
  }, [])
  const review = useQuery({
    queryKey: ['git_changes', run.id, run.state_version],
    queryFn: () => runsApi.gitChanges(run.id),
    refetchOnWindowFocus: false,
    retry: false,
  })
  const [selection, setSelection] = useState<{
    comparison: string
    paths: string[]
    acknowledged: boolean
    acceptHead?: boolean
  }>({
    comparison: '',
    paths: [],
    acknowledged: false,
  })
  const data = review.data
  const selected =
    data?.comparison_id === selection.comparison ? selection.paths : []
  const acknowledged =
    data?.comparison_id === selection.comparison && selection.acknowledged
  const acceptHead =
    data?.comparison_id === selection.comparison && !!selection.acceptHead
  const hasSelection = data?.kind === 'head' ? acceptHead : selected.length > 0
  const remaining = data
    ? data.changes.length + data.omitted_changes - selected.length
    : 0
  const incomplete =
    resumeAfterAcceptance && data?.kind === 'protected_files' && remaining > 0
  const disabled = busy || review.isFetching
  return (
    <form
      ref={form}
      className="git-changes-review"
      aria-label="Принятие изменений Git"
      onSubmit={(event) => {
        event.preventDefault()
        if (
          !data?.can_accept ||
          disabled ||
          !hasSelection ||
          incomplete ||
          !acknowledged ||
          data.state_version !== run.state_version
        )
          return
        onSubmit(
          {
            git_changes: {
              comparison_id: data.comparison_id,
              paths: selected,
              accept_head: acceptHead,
              acknowledge_risk: true,
            },
          },
          data,
        )
      }}
    >
      <h4>
        {data?.kind === 'head'
          ? 'Сравнение коммитов и принятие HEAD'
          : 'Сравнение защищённых файлов'}
      </h4>
      {resumeAfterAcceptance && (
        <p role="status">
          Ожидается ваше подтверждение.{' '}
          {data?.kind === 'head'
            ? 'Изучите сравнение, отметьте принятие HEAD и риск, затем нажмите «Принять HEAD и продолжить».'
            : 'Изучите сравнение ниже, выберите изменения файлов и подтвердите риск, затем нажмите «Принять выбранные изменения и продолжить».'}
        </p>
      )}
      <p>
        Оценка риска объясняет возможное влияние по типу файла и доступным
        данным. Она не гарантирует корректность изменений.
      </p>
      <button
        type="button"
        className="quiet"
        disabled={disabled}
        onClick={() => {
          setSelection({ comparison: '', paths: [], acknowledged: false })
          void review.refetch()
        }}
      >
        Обновить сравнение
      </button>
      {review.isPending && <p role="status">Сравниваем состояния…</p>}
      {review.error && (
        <p role="alert" className="error">
          {review.error.message}
        </p>
      )}
      {data && (
        <>
          <p className="hint">
            Рабочая область: <code>{data.workspace_path}</code>
          </p>
          <p>{data.effect}</p>
          {data.blockers.length > 0 && (
            <ul role="alert">
              {data.blockers.map((blocker) => (
                <li key={blocker}>{blocker}</li>
              ))}
            </ul>
          )}
          {data.head && <GitHeadComparison head={data.head} />}
          {!data.changes.length && data.kind !== 'head' && (
            <p>
              Расхождений защищённых файлов нет. При продолжении сервер повторно
              проверит условия Git-этапа.
            </p>
          )}
          {data.kind === 'head' ? (
            <label>
              <input
                type="checkbox"
                disabled={disabled || !data.can_accept}
                checked={acceptHead}
                onChange={(event) =>
                  setSelection({
                    comparison: data.comparison_id,
                    paths: [],
                    acknowledged: false,
                    acceptHead: event.target.checked,
                  })
                }
              />
              Принять текущий HEAD как основу следующего этапа
            </label>
          ) : (
            <fieldset disabled={disabled || !data.can_accept}>
              <legend>Выберите изменения для принятия</legend>
              {data.changes.map((file) => (
                <article key={file.path}>
                  <label>
                    <input
                      type="checkbox"
                      checked={selected.includes(file.path)}
                      onChange={(event) => {
                        setSelection({
                          comparison: data.comparison_id,
                          paths: event.target.checked
                            ? [...selected, file.path]
                            : selected.filter((path) => path !== file.path),
                          acknowledged: false,
                        })
                      }}
                    />
                    <strong>{file.path}</strong>
                  </label>
                  <p>
                    {changes[file.change]} · Риск: <b>{risks[file.risk]}</b>
                  </p>
                  <p>{file.assessment}</p>
                  <p>
                    Изменилось:{' '}
                    {file.changed_fields
                      .map((field) => fields[field] ?? field)
                      .join(', ') || 'состояние файла'}
                    .
                  </p>
                  <p>
                    Размер: {file.before?.size ?? 'нет'} →{' '}
                    {file.after?.size ?? 'нет'} байт. Игнорирование Git:{' '}
                    {file.before?.ignored ? 'да' : 'нет'} →{' '}
                    {file.after?.ignored ? 'да' : 'нет'}.
                  </p>
                  <p>{file.comparison_note}</p>
                  {typeof file.diff === 'string' && (
                    <details>
                      <summary>Разница в содержимом</summary>
                      <pre>
                        {file.diff ||
                          'Текстовые строки совпадают; отличаются метаданные или переводы строк.'}
                      </pre>
                      {file.diff_truncated && (
                        <p>
                          Показан фрагмент. Для решения изучите полный файл.
                        </p>
                      )}
                    </details>
                  )}
                </article>
              ))}
            </fieldset>
          )}
          {data.omitted_changes > 0 && (
            <p>
              Ещё расхождений: {data.omitted_changes}. Они не будут приняты этой
              командой.
            </p>
          )}
          {incomplete && (
            <p role="status">
              Для принятия с продолжением нужно проверить и выбрать все
              защищённые расхождения. Не выбрано: {remaining}. Остальные файлы
              автоматически не принимаются.
            </p>
          )}
          <label>
            <input
              type="checkbox"
              disabled={disabled || !hasSelection}
              checked={acknowledged}
              onChange={(event) => {
                setSelection({
                  comparison: data.comparison_id,
                  paths: selected,
                  acceptHead,
                  acknowledged: event.target.checked,
                })
              }}
            />
            Я изучил сравнение и принимаю риск выбранных изменений, включая
            указанные ограничения данных.
          </label>
        </>
      )}
      <footer>
        <button
          type="button"
          className="quiet"
          disabled={busy}
          onClick={onClose}
        >
          Закрыть
        </button>
        <button
          type="submit"
          disabled={
            disabled ||
            !data?.can_accept ||
            !hasSelection ||
            incomplete ||
            !acknowledged ||
            data.state_version !== run.state_version
          }
        >
          {data?.kind === 'head'
            ? resumeAfterAcceptance
              ? 'Принять HEAD и продолжить'
              : 'Принять HEAD'
            : resumeAfterAcceptance
              ? 'Принять выбранные изменения и продолжить'
              : 'Принять выбранные изменения'}
        </button>
      </footer>
      <p className="hint">
        {resumeAfterAcceptance
          ? data?.kind === 'head'
            ? 'После подтверждения помощник примет HEAD и сразу выполнит «Продолжить». Следующий этап агента сможет менять файлы. Если повторная проверка не пройдёт, оба действия будут отменены.'
            : 'После подтверждения помощник обновит эталон выбранных файлов и выполнит «Продолжить». Содержимое файлов не меняется, игнорируемые файлы в коммит не добавляются. Если повторная проверка не пройдёт, оба действия будут отменены.'
          : 'После принятия нажмите «Продолжить». Оставшиеся расхождения могут снова остановить этап.'}
      </p>
    </form>
  )
}
