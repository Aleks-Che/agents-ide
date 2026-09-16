import { useQuery } from '@tanstack/react-query'
import { planningApi, type PlanningSource } from '../../api/planning'
import { confirmedPlans } from './confirmed_plans'
import { describeBindingError } from '../../api/bindings'

export function ConfirmedPlanSelect({
  projectId,
  chatId,
  value,
  onChange,
}: {
  projectId: string
  chatId: string
  value: PlanningSource | null
  onChange: (value: PlanningSource | null) => void
}) {
  const jobs = useQuery({
    queryKey: ['confirmed_plans', projectId, chatId],
    queryFn: () => planningApi.list({ projectId, includeCompleted: true }),
    staleTime: 0,
  })
  const options = confirmedPlans(jobs.data ?? [], projectId, chatId)
  const key = value ? `${value.job_id}:${value.revision_number}` : ''
  const selected = options.find(
    (item) => `${item.job.id}:${item.revision.revision_number}` === key,
  )
  return (
    <section aria-label="Входной план Council">
      <label className="schema-field">
        Подтверждённый план Council
        <select
          value={key}
          onChange={(e) =>
            onChange(
              options.find(
                (item) =>
                  `${item.job.id}:${item.revision.revision_number}` ===
                  e.target.value,
              )?.source ?? null,
            )
          }
        >
          <option value="">План из входов версии / ручной ввод</option>
          {value && !selected && (
            <option value={key}>
              Переданный план · ревизия {value.revision_number} (проверит
              сервер)
            </option>
          )}
          {options.map(({ job, revision }) => (
            <option
              key={job.id}
              value={`${job.id}:${revision.revision_number}`}
            >
              {job.task_text.slice(0, 70)} · ревизия {revision.revision_number}
            </option>
          ))}
        </select>
      </label>
      {jobs.error && (
        <p role="alert" className="error">
          {describeBindingError(jobs.error)}
        </p>
      )}
      {jobs.isLoading && <p role="status">Загружаем подтверждённые планы…</p>}
      {selected && (
        <details>
          <summary>Содержание подтверждённой ревизии</summary>
          <pre>{selected.revision.body_text}</pre>
        </details>
      )}
      <p className="hint">
        Доступны только явно подтверждённые ревизии. Выбор передаёт источник
        плана; сервер проверит подтверждение и зафиксирует PlanItem при запуске.
      </p>
    </section>
  )
}
