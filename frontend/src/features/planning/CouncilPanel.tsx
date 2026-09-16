import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { connectionsApi, groupsApi } from '../../api/settings'
import {
  planningApi,
  type PlanningAnswerInput,
  type PlanningJobCreatePayload,
  type PlanningJobView,
  type PlanningMemberSpec,
  type PlanningQuestion,
  type PlanningSource,
} from '../../api/planning'
import { Modal } from '../../app/Modal'
import { useCsrfToken } from '../../app/session'
import { launchError, uncertainStart } from '../runs/launch'

const blankMember = (role: 'participant' | 'merger'): PlanningMemberSpec => ({
  role,
  selection: { kind: 'direct', model_id: '', provider_connection_id: '' },
})

export function CouncilPanel({
  projectId,
  chatId,
  onClose,
  onLaunched,
}: {
  projectId: string
  chatId: string | null
  open: boolean
  onClose: () => void
  onLaunched: (job: PlanningJobView) => void
}) {
  const csrf = useCsrfToken()
  const [taskText, setTaskText] = useState('')
  const [contextText, setContextText] = useState('')
  const [participants, setParticipants] = useState(() =>
    Array.from({ length: 3 }, () => blankMember('participant')),
  )
  const [merger, setMerger] = useState(() => blankMember('merger'))
  const key = `agents-ide.council-pending.${projectId}.${chatId}`
  const [stored] = useState(() => {
    try {
      const text = sessionStorage.getItem(key)
      return {
        body: text ? (JSON.parse(text) as PlanningJobCreatePayload) : null,
        error: null,
      }
    } catch {
      return {
        body: null,
        error:
          'Не удалось прочитать сохранённый запрос. Восстановите хранилище браузера.',
      }
    }
  })
  const [pending, setPending] = useState(stored.body)
  const resources = useQuery({
    queryKey: ['council-resources'],
    queryFn: async () => ({
      providers: await connectionsApi.list(),
      groups: await groupsApi.list({ kind: 'llm' }),
    }),
  })
  const create = useMutation({
    mutationFn: async () => {
      const body = pending ?? {
        project_id: projectId,
        chat_id: chatId,
        task_text: taskText.trim(),
        context_text: contextText,
        participants: [...participants, merger],
        idempotency_key: crypto.randomUUID(),
      }
      sessionStorage.setItem(key, JSON.stringify(body))
      setPending(body)
      return planningApi.create(body, csrf)
    },
    onSuccess: (job) => {
      sessionStorage.removeItem(key)
      onLaunched(job)
    },
    onError: (error) => {
      if (!uncertainStart(error)) {
        sessionStorage.removeItem(key)
        setPending(null)
      }
    },
  })
  const valid = (spec: PlanningMemberSpec) =>
    spec.selection.kind === 'group'
      ? Boolean(spec.selection.group_id)
      : Boolean(
          spec.selection.model_id.trim() &&
          'provider_connection_id' in spec.selection &&
          spec.selection.provider_connection_id,
        )
  const locked = create.isPending || Boolean(pending)
  const editor = (
    spec: PlanningMemberSpec,
    title: string,
    onChange: (next: PlanningMemberSpec) => void,
  ) => (
    <fieldset className="council-member" disabled={locked}>
      <legend>{title}</legend>
      <label>
        Тип выбора
        <select
          value={spec.selection.kind}
          onChange={(e) =>
            onChange({
              role: spec.role,
              selection:
                e.target.value === 'group'
                  ? { kind: 'group', group_id: '' }
                  : {
                      kind: 'direct',
                      model_id: '',
                      provider_connection_id: '',
                    },
            })
          }
        >
          <option value="direct">Прямая модель LLM</option>
          <option value="group">Группа LLM</option>
        </select>
      </label>
      {spec.selection.kind === 'group' ? (
        <label>
          Группа
          <select
            value={spec.selection.group_id}
            onChange={(e) =>
              onChange({
                ...spec,
                selection: { kind: 'group', group_id: e.target.value },
              })
            }
          >
            <option value="">Выберите группу</option>
            {resources.data?.groups
              .filter((g) => !g.archived)
              .map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                </option>
              ))}
          </select>
        </label>
      ) : (
        <>
          <label>
            Модель
            <input
              value={spec.selection.model_id}
              onChange={(e) =>
                onChange({
                  ...spec,
                  selection: {
                    ...spec.selection,
                    model_id: e.target.value,
                  } as PlanningMemberSpec['selection'],
                })
              }
            />
          </label>
          <label>
            Подключение
            <select
              value={
                'provider_connection_id' in spec.selection
                  ? spec.selection.provider_connection_id
                  : ''
              }
              onChange={(e) =>
                onChange({
                  ...spec,
                  selection: {
                    kind: 'direct',
                    model_id:
                      spec.selection.kind === 'direct'
                        ? spec.selection.model_id
                        : '',
                    provider_connection_id: e.target.value,
                  },
                })
              }
            >
              <option value="">Выберите подключение</option>
              {resources.data?.providers
                .filter((p) => !p.archived)
                .map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
            </select>
          </label>
        </>
      )}
    </fieldset>
  )
  return (
    <Modal onClose={onClose} busy={create.isPending} labelledBy="council-title">
      <div className="dialog wide council-dialog">
        <h2 id="council-title">Совместное планирование</h2>
        <p className="hint">
          Участники получают одинаковую задачу и текст контекста ниже. Доступа к
          файлам проекта и инструментам нет. Сейчас доступны LLM-подключения и
          группы LLM; harness требует отдельной приёмки.
        </p>
        <label>
          Задача
          <textarea
            aria-label="Текст задачи"
            rows={4}
            value={taskText}
            maxLength={65536}
            disabled={locked}
            onChange={(e) => setTaskText(e.target.value)}
          />
        </label>
        <label>
          Контекст для всех участников
          <textarea
            rows={4}
            value={contextText}
            maxLength={65536}
            disabled={locked}
            onChange={(e) => setContextText(e.target.value)}
          />
        </label>
        <label>
          Число участников
          <select
            disabled={locked}
            value={participants.length}
            onChange={(e) =>
              setParticipants((prev) =>
                Array.from(
                  { length: Number(e.target.value) },
                  (_, i) => prev[i] ?? blankMember('participant'),
                ),
              )
            }
          >
            {[2, 3, 4].map((n) => (
              <option key={n}>{n}</option>
            ))}
          </select>
        </label>
        {participants.map((spec, i) => (
          <div key={i}>
            {editor(spec, `Участник ${i + 1}`, (next) =>
              setParticipants((prev) =>
                prev.map((p, k) => (k === i ? next : p)),
              ),
            )}
          </div>
        ))}
        {editor(merger, 'Объединяющий', setMerger)}
        {resources.isLoading ? (
          <p role="status">Загружаем подключения…</p>
        ) : null}
        {resources.error ? (
          <p role="alert">
            {launchError(resources.error)}{' '}
            <button onClick={() => void resources.refetch()}>
              Повторить загрузку
            </button>
          </p>
        ) : null}
        {stored.error || create.error ? (
          <p role="alert">{stored.error ?? launchError(create.error)}</p>
        ) : null}
        {pending ? (
          <>
            <p className="hint">
              Исход запроса неизвестен. Повтор отправит исходную задачу с тем же
              ключом.
            </p>
            <details>
              <summary>Сохранённая задача для повтора</summary>
              <pre>{pending.task_text}</pre>
              <pre>{pending.context_text}</pre>
            </details>
          </>
        ) : null}
        <footer>
          <button type="button" className="quiet" onClick={onClose}>
            Закрыть
          </button>
          <button
            type="button"
            onClick={() => create.mutate()}
            disabled={
              create.isPending ||
              !csrf ||
              Boolean(stored.error) ||
              (!pending &&
                (!taskText.trim() ||
                  !participants.every(valid) ||
                  !valid(merger) ||
                  !resources.data))
            }
          >
            {pending ? 'Повторить создание' : 'Составить план'}
          </button>
        </footer>
      </div>
    </Modal>
  )
}

export function CouncilReviewer({
  job,
  onClose,
  onUse,
}: {
  job: PlanningJobView
  onClose: () => void
  onUse: (source: PlanningSource) => void
}) {
  const revision = job.revisions?.at(-1)
  return (
    <ReviewForm
      key={`${job.id}:${revision?.revision_number ?? 0}`}
      job={job}
      onClose={onClose}
      onUse={onUse}
    />
  )
}

function ReviewForm({
  job,
  onClose,
  onUse,
}: {
  job: PlanningJobView
  onClose: () => void
  onUse: (source: PlanningSource) => void
}) {
  const csrf = useCsrfToken()
  const client = useQueryClient()
  const revision = job.revisions?.at(-1)
  const [answers, setAnswers] = useState<Record<string, PlanningAnswerInput>>(
    {},
  )
  const [editing, setEditing] = useState(false)
  const [body, setBody] = useState(() =>
    JSON.stringify(
      {
        body_text: revision?.body_text ?? '',
        steps:
          revision?.plan_items?.map((item) => ({
            title: item.title,
            acceptance_criteria: item.acceptance_criteria,
          })) ?? [],
        questions: revision?.questions ?? [],
      },
      null,
      2,
    ),
  )
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ['planning-job', job.id] })
    void client.invalidateQueries({ queryKey: ['planning-jobs'] })
  }
  const submit = useMutation({
    mutationFn: () =>
      planningApi.submitAnswers(
        job.id,
        {
          expected_revision: revision!.revision_number,
          answers: Object.values(answers),
          user_body_text: editing ? body : null,
        },
        csrf,
      ),
    onSuccess: refresh,
  })
  const confirm = useMutation({
    mutationFn: () =>
      planningApi.confirm(
        job.id,
        {
          expected_revision: revision!.revision_number,
          confirmation_hash: revision!.confirmation_hash!,
        },
        csrf,
      ),
    onSuccess: refresh,
  })
  const cancel = useMutation({
    mutationFn: () =>
      planningApi.cancel(
        job.id,
        { expected_state_version: job.state_version },
        csrf,
      ),
    onSuccess: refresh,
  })
  const terminal = ['confirmed', 'cancelled', 'failed'].includes(job.state)
  const revisable = ['ready_for_confirmation', 'needs_answers'].includes(
    job.state,
  )
  const questions = revision?.questions ?? []
  const answered = questions.every((q) => {
    const a = answers[q.id]
    return (
      a &&
      (q.kind === 'text'
        ? Boolean(a.free_text?.trim())
        : Boolean(a.selected_option_ids?.length))
    )
  })
  const busy = submit.isPending || confirm.isPending || cancel.isPending
  return (
    <Modal onClose={onClose} busy={busy} labelledBy="council-review-title">
      <div className="dialog wide council-dialog">
        <h2 id="council-review-title">План совета моделей</h2>
        <p>
          Состояние: {job.state} · приняты участники:{' '}
          {job.n_participants_actual}/{job.n_participants_requested}
          {job.degraded ? ' · уменьшенный состав' : ''}
        </p>
        <p>
          Вызовы: {String(job.usage?.external_calls ?? 0)} · токены:{' '}
          {String(job.usage?.tokens_used ?? 'неизвестно')} · стоимость:{' '}
          {String(job.usage?.cost_estimated ?? 'неизвестно')}
        </p>
        <ul>
          {job.members?.map((m) => (
            <li key={m.id}>
              {m.role === 'merger'
                ? 'Объединяющий'
                : `Участник ${m.slot_index + 1}`}
              : {m.selected_model_id || 'модель ещё не выбрана'} · {m.status}
              {m.selection.kind === 'group'
                ? ` · группа ${String(m.selection.group_name ?? m.selection.group_id)} / ревизия ${String(m.selection.group_revision ?? '?')}`
                : ''}
              {m.selected_connection_id
                ? ` · подключение ${m.selected_connection_name ?? m.selected_connection_id.slice(0, 8)}`
                : ''}
              {m.error ? ` · ${JSON.stringify(m.error)}` : ''}
            </li>
          ))}
        </ul>
        {job.last_error ? (
          <p role="alert">{JSON.stringify(job.last_error)}</p>
        ) : null}
        <h3>Черновики</h3>
        {job.events?.length ? (
          <details>
            <summary>Последние события и причины переключений</summary>
            <ol>
              {job.events.map((e) => (
                <li key={e.sequence}>
                  {e.type} · {JSON.stringify(e.payload)}
                </li>
              ))}
            </ol>
          </details>
        ) : null}
        {job.drafts?.map((d) => (
          <details key={d.member_id}>
            <summary>
              {d.model_id} · {d.accepted ? 'принят' : 'не принят'} ·{' '}
              {d.byte_length} байт
            </summary>
            <pre>{d.body_text}</pre>
            {d.body_truncated ? <p>Ответ усечён и не принят.</p> : null}
          </details>
        ))}
        {revision ? (
          <>
            <h3>
              Ревизия {revision.revision_number} ·{' '}
              {revision.confirmed_at ? 'подтверждена' : 'не подтверждена'}
            </h3>
            <pre className="council-plan">{revision.body_text}</pre>
            <ol>
              {revision.plan_items?.map((item) => (
                <li key={String(item.id)}>
                  <strong>
                    {String(item.id)}: {String(item.title)}
                  </strong>
                  <ul>
                    {(item.acceptance_criteria as string[]).map((c, i) => (
                      <li key={i}>{c}</li>
                    ))}
                  </ul>
                </li>
              ))}
            </ol>
            {revision.answers?.length ? (
              <details>
                <summary>Зафиксированные ответы</summary>
                <pre>{JSON.stringify(revision.answers, null, 2)}</pre>
              </details>
            ) : null}
            {revisable ? (
              <fieldset disabled={busy}>
                {questions.map((q) => (
                  <QuestionField
                    key={q.id}
                    question={q}
                    value={answers[q.id]}
                    onChange={(a) =>
                      setAnswers((prev) => ({ ...prev, [q.id]: a }))
                    }
                  />
                ))}
                <label>
                  <input
                    type="checkbox"
                    checked={editing}
                    onChange={(e) => setEditing(e.target.checked)}
                  />
                  Уточнить план вручную
                </label>
                {editing ? (
                  <label>
                    Полный план JSON
                    <textarea
                      rows={10}
                      value={body}
                      onChange={(e) => setBody(e.target.value)}
                    />
                  </label>
                ) : null}
                <button
                  onClick={() => submit.mutate()}
                  disabled={
                    !csrf || !answered || (!editing && !questions.length)
                  }
                >
                  Сохранить ответы и правки
                </button>
                {job.state === 'ready_for_confirmation' ? (
                  <button
                    onClick={() => confirm.mutate()}
                    disabled={
                      !csrf ||
                      editing ||
                      Object.keys(answers).length > 0 ||
                      !revision.confirmation_hash ||
                      revision.readiness !== 'ready'
                    }
                  >
                    Подтвердить план
                  </button>
                ) : null}
              </fieldset>
            ) : null}
            {job.state === 'confirmed' && revision.confirmation_hash ? (
              <button
                onClick={() =>
                  onUse({
                    job_id: job.id,
                    revision_number: revision.revision_number,
                    confirmation_hash: revision.confirmation_hash!,
                  })
                }
              >
                Использовать план в Run…
              </button>
            ) : null}
          </>
        ) : (
          <p role="status">
            {terminal
              ? 'Подготовка завершена без готового плана.'
              : 'Получаем и объединяем черновики…'}
          </p>
        )}
        {[submit.error, confirm.error, cancel.error]
          .filter(Boolean)
          .map((error, i) => (
            <p role="alert" key={i}>
              {launchError(error)}
            </p>
          ))}
        <footer>
          {!terminal ? (
            <button
              className="quiet danger"
              disabled={!csrf || busy}
              onClick={() => cancel.mutate()}
            >
              Отменить подготовку
            </button>
          ) : null}
          <button onClick={onClose}>Закрыть</button>
        </footer>
      </div>
    </Modal>
  )
}

function QuestionField({
  question: q,
  value,
  onChange,
}: {
  question: PlanningQuestion
  value?: PlanningAnswerInput
  onChange: (a: PlanningAnswerInput) => void
}) {
  const update = (patch: Partial<PlanningAnswerInput>) =>
    onChange({
      question_id: q.id,
      kind: q.kind,
      selected_option_ids: value?.selected_option_ids ?? [],
      free_text: value?.free_text ?? '',
      ...patch,
    })
  return (
    <div className="council-question">
      <label>
        {q.prompt}
        {q.kind === 'text' ? (
          <textarea
            value={value?.free_text ?? ''}
            maxLength={4000}
            onChange={(e) => update({ free_text: e.target.value })}
          />
        ) : (
          <select
            multiple={q.kind === 'multi'}
            value={
              q.kind === 'multi'
                ? (value?.selected_option_ids ?? [])
                : (value?.selected_option_ids?.[0] ?? '')
            }
            onChange={(e) =>
              update({
                selected_option_ids: Array.from(e.target.selectedOptions)
                  .map((o) => o.value)
                  .filter(Boolean),
              })
            }
          >
            {q.kind === 'single' ? (
              <option value="">Выберите ответ</option>
            ) : null}
            {q.options?.map((o) => (
              <option key={o.id} value={o.id}>
                {o.label}
              </option>
            ))}
          </select>
        )}
      </label>
      {q.allow_text && q.kind !== 'text' ? (
        <label>
          Комментарий к ответу: {q.prompt}
          <textarea
            maxLength={4000}
            value={value?.free_text ?? ''}
            onChange={(e) => update({ free_text: e.target.value })}
          />
        </label>
      ) : null}
    </div>
  )
}
