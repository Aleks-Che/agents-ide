import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  planningApi,
  type PlanningCouncilDefaults,
  type PlanningMemberSpec,
} from '../../api/planning'
import { connectionsApi, groupsApi, harnessApi } from '../../api/settings'
import { useCsrfToken } from '../../app/session'
import { launchError } from '../runs/launch'
import { CouncilMemberEditor } from '../planning/CouncilMemberEditor'
import { blankMember, validCouncilMember } from '../planning/council_members'

export function PlanningCouncilPanel() {
  const defaults = useQuery({
    queryKey: ['council-defaults'],
    queryFn: planningApi.defaults,
  })
  return (
    <section className="panel" aria-labelledby="planning-council-heading">
      <header className="panel-header">
        <span className="section-label" id="planning-council-heading">
          Совет планирования
        </span>
      </header>
      <p className="hint">
        Выберите двух или трёх участников и модель, которая объединит их
        предложения. Этот состав подставляется при новом планировании в любом
        проекте.
      </p>
      {defaults.isLoading ? (
        <p role="status">Загружаем состав совета…</p>
      ) : null}
      {defaults.error ? (
        <p role="alert">
          {launchError(defaults.error)}{' '}
          <button type="button" onClick={() => void defaults.refetch()}>
            Повторить загрузку
          </button>
        </p>
      ) : null}
      {defaults.data ? <CouncilDefaultsForm initial={defaults.data} /> : null}
    </section>
  )
}

function CouncilDefaultsForm({
  initial,
}: {
  initial: PlanningCouncilDefaults
}) {
  const csrf = useCsrfToken()
  const client = useQueryClient()
  const [saved, setSaved] = useState(false)
  const [revision, setRevision] = useState(initial.revision)
  const [participants, setParticipants] = useState(() =>
    initial.participants?.length
      ? initial.participants.filter((member) => member.role === 'participant')
      : Array.from({ length: 2 }, () => blankMember('participant')),
  )
  const [merger, setMerger] = useState(
    () =>
      initial.participants?.find((member) => member.role === 'merger') ??
      blankMember('merger'),
  )
  const resources = useQuery({
    queryKey: ['council-resources'],
    queryFn: async () => {
      const [providers, harnesses, groups] = await Promise.all([
        connectionsApi.list(),
        harnessApi.list(),
        groupsApi.list({}),
      ])
      return { providers, harnesses, groups }
    },
  })
  const save = useMutation({
    mutationFn: () =>
      planningApi.saveDefaults(
        { revision, participants: [...participants, merger] },
        csrf,
      ),
    onSuccess: (updated) => {
      setRevision(updated.revision)
      client.setQueryData(['council-defaults'], updated)
    },
  })
  const update = (index: number, next: PlanningMemberSpec) => {
    setSaved(false)
    setParticipants((prev) =>
      prev.map((member, i) => (i === index ? next : member)),
    )
  }
  return (
    <form
      className="council-defaults"
      onSubmit={(event) => {
        event.preventDefault()
        setSaved(true)
        save.mutate()
      }}
    >
      <fieldset className="council-default-fields" disabled={save.isPending}>
        <label>
          Число участников
          <select
            value={participants.length}
            onChange={(event) => {
              setSaved(false)
              setParticipants((prev) =>
                Array.from(
                  { length: Number(event.target.value) },
                  (_, i) => prev[i] ?? blankMember('participant'),
                ),
              )
            }}
          >
            {[2, 3].map((count) => (
              <option key={count}>{count}</option>
            ))}
          </select>
        </label>
        {participants.map((member, i) => (
          <CouncilMemberEditor
            key={i}
            spec={member}
            title={`Участник ${i + 1}`}
            resources={resources.data}
            onChange={(next) => update(i, next)}
          />
        ))}
        <CouncilMemberEditor
          spec={merger}
          title="Объединяющий"
          resources={resources.data}
          onChange={(next) => {
            setSaved(false)
            setMerger(next)
          }}
        />
        <p className="hint">
          Можно выбрать агента Codex/OpenCode, LLM или группу с приоритетами и
          расписаниями. Перед запуском состав можно изменить.
        </p>
        {resources.error || save.error ? (
          <p role="alert">{launchError(resources.error ?? save.error)}</p>
        ) : null}
        {saved && save.isSuccess ? (
          <p role="status">Состав совета сохранён.</p>
        ) : null}
        <button
          type="submit"
          disabled={
            !csrf ||
            !resources.data ||
            ![...participants, merger].every(validCouncilMember)
          }
        >
          {save.isPending ? 'Сохраняем…' : 'Сохранить состав совета'}
        </button>
      </fieldset>
    </form>
  )
}
