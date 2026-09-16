import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { graphsApi, type GraphDocument } from '../../api/graphs'
import { groupsApi, type ModelGroupExport } from '../../api/settings'
import { describeBindingError } from '../../api/bindings'
import { useCsrfToken } from '../../app/session'
import { Destination } from './ModelSelectionEditor'
import type { EditorResources } from './resources'
import { resourceReferences, remapResources } from './transfer'
import { documentFrom, downloadJson, object } from './graph'

async function readJson(file: File) {
  if (file.size > 1048576) throw new Error('Файл превышает 1 MiB.')
  return JSON.parse(await file.text()) as unknown
}

export function GraphTransfer({
  resources,
  onImport,
}: {
  resources: EditorResources
  onImport: (doc: GraphDocument) => void
}) {
  const csrf = useCsrfToken()
  const [imported, setImported] = useState<GraphDocument | null>(null)
  const [mapping, setMapping] = useState<Record<string, string>>({})
  const [reviewedKey, setReviewedKey] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const refs = imported ? resourceReferences(imported) : []
  const mapped = imported ? remapResources(imported, mapping) : null
  const key = JSON.stringify([mapped, resources])
  const complete = refs.every((ref) => Boolean(mapping[ref.key]))
  const apply = useMutation({
    mutationFn: async () => {
      const checked = await graphsApi.import(mapped, csrf)
      if (!checked.ok || !checked.body)
        throw new Error(checked.errors.map((e) => e.message).join('\n'))
      const doc = documentFrom(checked.body)
      // Validate local references and every candidate destination after explicit mapping.
      const validation = await graphsApi.validate(doc, csrf)
      if (!validation.ok)
        throw new Error(validation.errors.map((e) => e.message).join('\n'))
      return doc
    },
    onSuccess: (doc) => {
      onImport(doc)
      setImported(null)
      setMapping({})
      setReviewedKey('')
    },
  })
  return (
    <section aria-label="Импорт графа">
      <h3>Импорт графа</h3>
      <label className="schema-field">
        Файл графа
        <input
          type="file"
          accept=".json,application/json"
          disabled={busy || apply.isPending}
          onChange={async (e) => {
            const file = e.target.files?.[0]
            if (!file) return
            setBusy(true)
            setError('')
            setImported(null)
            setMapping({})
            setReviewedKey('')
            apply.reset()
            try {
              const report = await graphsApi.import(await readJson(file), csrf)
              if (!report.ok || !report.body)
                throw new Error(
                  report.errors
                    .map(
                      (issue) =>
                        `${issue.node_id ?? issue.edge_id ?? ''}: ${issue.message}`,
                    )
                    .join('\n'),
                )
              setImported(documentFrom(report.body))
            } catch (err) {
              setError(describeBindingError(err))
            } finally {
              setBusy(false)
              e.target.value = ''
            }
          }}
        />
      </label>
      {busy && <p role="status">Проверяем файл на сервере…</p>}
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {imported && (
        <>
          <p>
            Сопоставьте все ссылки с локальными ресурсами, включая ссылки на
            этой установке.
          </p>
          {refs.map((ref) => (
            <label className="schema-field" key={ref.key}>
              {ref.type} / {ref.kind}: {ref.id}
              <select
                value={mapping[ref.key] ?? ''}
                onChange={(e) => {
                  setMapping({ ...mapping, [ref.key]: e.target.value })
                  apply.reset()
                }}
              >
                <option value="">Выберите локальный ресурс…</option>
                {(ref.type === 'group'
                  ? resources.groups.filter((g) => g.kind === ref.kind)
                  : ref.type === 'harness'
                    ? resources.harnesses
                    : resources.connections
                )
                  .filter((r) => !r.archived)
                  .map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.name}
                    </option>
                  ))}
              </select>
            </label>
          ))}
          {mapped && <ExecutionReview doc={mapped} resources={resources} />}
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={reviewedKey === key}
              disabled={!complete}
              onChange={(e) => setReviewedKey(e.target.checked ? key : '')}
            />
            Я проверил программы, аргументы, роли, пути и назначения всех
            кандидатов
          </label>
          <p className="hint">
            Доверие из файла не переносится. Перед запуском потребуется
            подтвердить execution_hash из preflight. Правки исполнения требуют
            нового подтверждения.
          </p>
          {apply.error && (
            <p role="alert" className="error">
              {describeBindingError(apply.error)}
            </p>
          )}
          <button
            type="button"
            disabled={!complete || reviewedKey !== key || apply.isPending}
            onClick={() => apply.mutate()}
          >
            Применить импорт к черновику
          </button>
        </>
      )}
      <GroupTransfer resources={resources} />
    </section>
  )
}

export function ExecutionReview({
  doc,
  resources,
}: {
  doc: GraphDocument
  resources: EditorResources
}) {
  const refs = resourceReferences(doc)
  const groups = resources.groups.filter((g) =>
    refs.some((r) => r.type === 'group' && r.id === g.id),
  )
  return (
    <details open className="execution-review">
      <summary>Программы, argv, роли, пути и назначения данных</summary>
      <pre>
        {JSON.stringify(
          {
            schema_version: doc.schema_version,
            required_features: doc.required_features,
            roles: doc.graph.roles,
            settings: doc.settings,
            inputs: doc.inputs,
            nodes: doc.graph.nodes.map(({ id, type, config, expression }) => ({
              id,
              type,
              config,
              expression,
            })),
            transitions: doc.graph.edges.map(
              ({ source, target, when, loop, assignments }) => ({
                source,
                target,
                when,
                loop,
                assignments,
              }),
            ),
          },
          null,
          2,
        )}
      </pre>
      {refs
        .filter((r) => r.type !== 'group')
        .map((r) => (
          <Destination
            key={r.key}
            resources={resources}
            harnessId={r.type === 'harness' ? r.id : null}
            connectionId={r.type === 'connection' ? r.id : null}
          />
        ))}
      {groups.map((group) => (
        <section key={group.id}>
          <h4>
            {group.name} · {group.kind} · ревизия {group.revision}
          </h4>
          <ol>
            {[...group.members]
              .sort((a, b) => a.member_index - b.member_index)
              .map((member) => (
                <li key={member.id}>
                  {member.model_id} · {member.enabled ? 'включён' : 'отключён'}
                  <Destination
                    resources={resources}
                    harnessId={member.harness_profile_id}
                    connectionId={member.provider_connection_id}
                  />
                  <pre>{JSON.stringify(member.params, null, 2)}</pre>
                </li>
              ))}
          </ol>
        </section>
      ))}
      <p className="hint">
        Программы и назначения будут повторно разрешены сервером для конкретного
        проекта при preflight. Просмотрите также резервных и отключённых
        кандидатов.
      </p>
    </details>
  )
}

function GroupTransfer({ resources }: { resources: EditorResources }) {
  const csrf = useCsrfToken(),
    client = useQueryClient()
  const [definition, setDefinition] = useState<ModelGroupExport | null>(null)
  const [bindings, setBindings] = useState<Record<string, string>>({})
  const [name, setName] = useState(''),
    [error, setError] = useState(''),
    [reviewed, setReviewed] = useState('')
  const [busy, setBusy] = useState(false)
  const reviewKey = JSON.stringify([definition, bindings, name, resources])
  const importGroup = useMutation({
    mutationFn: () =>
      groupsApi.import(
        { definition: definition!, resource_bindings: bindings, name },
        csrf,
      ),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ['graph_editor_resources'] })
      await client.invalidateQueries({ queryKey: ['model_groups'] })
      setDefinition(null)
    },
  })
  const exportGroup = useMutation({
    mutationFn: async (id: string) =>
      downloadJson(await groupsApi.export(id), `group-${id}.json`),
  })
  return (
    <details>
      <summary>Перенос групп моделей</summary>
      <label className="schema-field">
        Экспорт группы
        <select
          value=""
          onChange={(e) => {
            if (e.target.value) exportGroup.mutate(e.target.value)
          }}
        >
          <option value="">Выберите группу…</option>
          {resources.groups
            .filter((g) => !g.archived)
            .map((g) => (
              <option key={g.id} value={g.id}>
                {g.name} ({g.kind})
              </option>
            ))}
        </select>
      </label>
      <label className="schema-field">
        Файл группы
        <input
          type="file"
          accept=".json,application/json"
          disabled={busy || importGroup.isPending}
          onChange={async (e) => {
            const file = e.target.files?.[0]
            if (!file) return
            setBusy(true)
            setError('')
            setDefinition(null)
            setBindings({})
            setReviewed('')
            importGroup.reset()
            try {
              const raw = object(await readJson(file))
              if (
                !['agent', 'llm'].includes(String(raw.kind)) ||
                !Array.isArray(raw.members) ||
                !raw.members.length ||
                raw.members.length > 200 ||
                raw.members.some(
                  (m) => typeof object(m).resource_ref !== 'string',
                )
              )
                throw new Error('Некорректное определение группы.')
              setDefinition(raw as unknown as ModelGroupExport)
              setName(String(raw.name ?? '').slice(0, 120))
            } catch (err) {
              setError(describeBindingError(err))
            } finally {
              setBusy(false)
              e.target.value = ''
            }
          }}
        />
      </label>
      {(error || importGroup.error || exportGroup.error) && (
        <p role="alert" className="error">
          {error ||
            describeBindingError(importGroup.error ?? exportGroup.error)}
        </p>
      )}
      {definition && (
        <>
          <label className="schema-field">
            Название импортированной группы
            <input
              value={name}
              maxLength={120}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          {[...new Set(definition.members.map((m) => m.resource_ref))].map(
            (ref) => (
              <label key={ref} className="schema-field">
                Ресурс: {ref}
                <select
                  value={bindings[ref] ?? ''}
                  onChange={(e) =>
                    setBindings({ ...bindings, [ref]: e.target.value })
                  }
                >
                  <option value="">Выберите…</option>
                  {(definition.kind === 'agent'
                    ? resources.harnesses
                    : resources.connections
                  )
                    .filter((r) => !r.archived)
                    .map((r) => (
                      <option key={r.id} value={r.id}>
                        {r.name}
                      </option>
                    ))}
                </select>
              </label>
            ),
          )}
          <ol>
            {definition.members.map((m, index) => (
              <li key={index}>
                {index + 1}. {m.model_id} ·{' '}
                {m.enabled === false ? 'отключён' : 'включён'}
                <Destination
                  resources={resources}
                  harnessId={
                    definition.kind === 'agent'
                      ? bindings[m.resource_ref]
                      : null
                  }
                  connectionId={
                    definition.kind === 'llm' ? bindings[m.resource_ref] : null
                  }
                />
                <pre>{JSON.stringify(m.params ?? {}, null, 2)}</pre>
              </li>
            ))}
          </ol>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={reviewed === reviewKey}
              onChange={(e) => setReviewed(e.target.checked ? reviewKey : '')}
            />
            Проверены назначения всех кандидатов группы
          </label>
          <button
            type="button"
            disabled={
              !name.trim() ||
              reviewed !== reviewKey ||
              importGroup.isPending ||
              definition.members.some((m) => !bindings[m.resource_ref])
            }
            onClick={() => importGroup.mutate()}
          >
            Импортировать группу
          </button>
        </>
      )}
    </details>
  )
}
