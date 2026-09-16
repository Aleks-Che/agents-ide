import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { describeRunError, runsApi } from '../../api/runs'

export function ArtifactDetail({
  runId,
  artifactId,
}: {
  runId: string
  artifactId: string
}) {
  const [offset, setOffset] = useState(0)
  const [format, setFormat] = useState<'text' | 'json'>('text')
  const detail = useQuery({
    queryKey: ['run_artifact_content', runId, artifactId, offset, format],
    queryFn: () => runsApi.artifactContent(runId, artifactId, offset, format),
    gcTime: 0,
    staleTime: Infinity,
  })
  const data = detail.data
  return (
    <div className="artifact-detail">
      {detail.isLoading ? <p role="status">Загружаем артефакт…</p> : null}
      {detail.error ? (
        <p role="alert">
          {describeRunError(detail.error)}{' '}
          <button onClick={() => void detail.refetch()}>
            Повторить загрузку
          </button>
        </p>
      ) : null}
      {data ? (
        <>
          <h5>
            {data.artifact.schema_type} · {data.artifact.source_kind}
          </h5>
          <p>
            Шаг {data.artifact.step_execution_id ?? '—'} · попытка{' '}
            {data.artifact.step_attempt_id ?? '—'}
          </p>
          <label>
            Формат артефакта
            <select
              value={format}
              onChange={(event) => {
                setFormat(event.target.value as 'text' | 'json')
                setOffset(0)
              }}
            >
              <option value="text">Текст / отчёт / diff</option>
              <option value="json">JSON</option>
            </select>
          </label>
          <pre className="artifact-body">{data.text}</pre>
          <p>
            Символы {offset + 1}–{Math.min(offset + 16000, data.total_chars)} из{' '}
            {data.total_chars}
          </p>
          <button
            disabled={!offset}
            onClick={() => setOffset(Math.max(0, offset - 16000))}
          >
            Предыдущий фрагмент
          </button>{' '}
          <button
            disabled={offset + 16000 >= data.total_chars}
            onClick={() => setOffset(offset + 16000)}
          >
            Следующий фрагмент
          </button>
          {data.artifact.redaction.length ? (
            <p>Скрыто: {data.artifact.redaction.join(', ')}</p>
          ) : null}
          {data.artifact.truncation ? (
            <p>
              Содержимое усечено при сохранении:{' '}
              {JSON.stringify(data.artifact.truncation)}
            </p>
          ) : null}
        </>
      ) : null}
    </div>
  )
}
