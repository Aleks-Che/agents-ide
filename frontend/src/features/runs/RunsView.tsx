import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { runsApi } from '../../api/runs'
import { describeError } from '../../api/projects'
import { formatDateTime, shortHash, pluraliseRuns } from '../../app/format'
import type { Chat, Project } from '../../api/projects'
import { RunScreen } from './RunsPanel'

interface RunsViewProps {
  project: Project | null
  chats: Chat[]
  selectedChatId: string | null
  onSelectChat: (chatId: string | null) => void
}

export function RunsView({ project }: RunsViewProps) {
  const runs = useQuery({
    queryKey: ['runs_summary', { projectId: project?.id ?? '' }],
    queryFn: () => runsApi.list({ projectId: project?.id }),
    enabled: Boolean(project),
    refetchInterval: 5_000,
  })
  const [openRunId, setOpenRunId] = useState<string | null>(null)
  if (!project) {
    return (
      <section className="main-pane" aria-labelledby="runs-view-title">
        <header className="main-header">
          <div>
            <span className="eyebrow">ЗАПУСКИ</span>
            <h2 id="runs-view-title">Выберите проект</h2>
            <span className="muted">
              Список запусков и активных процессов появится после выбора проекта
              слева.
            </span>
          </div>
        </header>
      </section>
    )
  }
  return (
    <section className="main-pane" aria-labelledby="runs-view-title">
      <header className="main-header">
        <div>
          <span className="eyebrow">ЗАПУСКИ</span>
          <h2 id="runs-view-title">{project.name}</h2>
          <span className="muted">
            {runs.data ? `${pluraliseRuns(runs.data.length)} в проекте` : '…'}
          </span>
        </div>
      </header>
      {runs.isLoading ? (
        <p role="status" className="panel-empty">
          Загружаем список запусков…
        </p>
      ) : runs.error ? (
        <p className="error">{describeError(runs.error)}</p>
      ) : !runs.data?.length ? (
        <p className="panel-empty">
          Запусков пока нет. Откройте диалог проекта и нажмите «Запустить».
        </p>
      ) : (
        <ul className="panel-list" aria-label="Все запуски проекта">
          {runs.data.map((run) => (
            <li key={run.id} className="profile-item">
              <header>
                <strong>{shortHash(run.id, 12)}</strong>
                <span className="pill">{run.state}</span>
              </header>
              <span className="meta">
                <span>{formatDateTime(run.created_at)}</span>
                <span className="muted">
                  hash {shortHash(run.execution_hash, 12)}
                </span>
                <button
                  type="button"
                  className="quiet"
                  onClick={() => setOpenRunId(run.id)}
                >
                  Открыть
                </button>
              </span>
            </li>
          ))}
        </ul>
      )}
      {openRunId ? (
        <RunScreen
          key={openRunId}
          runId={openRunId}
          onClose={() => setOpenRunId(null)}
        />
      ) : null}
    </section>
  )
}
