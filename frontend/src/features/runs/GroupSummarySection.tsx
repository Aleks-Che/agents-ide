import type {
  SelectionNodeEntry,
  SelectionSummary,
  WaitingReason,
} from '../../api/runs'
import { CANDIDATE_STATE_LABELS } from '../../api/runs'

function reasonLabel(reason: unknown) {
  if (reason === 'outside_schedule') return 'Вне расписания'
  if (reason === 'schedule_invalid') return 'Ошибка расписания'
  return String(reason ?? '—')
}

export function GroupSummarySection({
  selection,
  waiting,
}: {
  selection: SelectionSummary
  waiting: WaitingReason | null | undefined
}) {
  const groups = selection.groups ?? []
  const nodes = selection.nodes ?? []
  const exhausted = waiting?.code === 'model_group_exhausted'
  const diagnosticNodeId = exhausted ? waiting.details?.node_id : undefined
  return (
    <section className="run-group-summary" aria-label="Группа и кандидаты">
      <h4>Группа и кандидаты</h4>
      {selection.group_changes_apply_only_to_new_runs ? (
        <p className="hint">
          В этом Run закреплены состав, порядок и параметры кандидатов.
          Изменения группы применятся только к новым Run.
        </p>
      ) : null}
      {groups.length ? (
        <ul className="group-list">
          {groups.map((group) => (
            <li key={group.id}>
              <strong>{group.name || group.id}</strong> · тип {group.kind} ·
              ревизия {group.revision} · {group.enabled_count} из{' '}
              {group.member_count} включено
            </li>
          ))}
        </ul>
      ) : (
        <p>Закреплённые группы отсутствуют.</p>
      )}
      {exhausted ? (
        <div className="candidate-diagnostics">
          <h5>
            Диагностика исчерпания
            {typeof diagnosticNodeId === 'string'
              ? ` · узел ${diagnosticNodeId}`
              : ''}
          </h5>
          <p>
            Причина для каждой позиции указана в таблице узла. После
            восстановления доступа к закреплённым ресурсам нажмите «Продолжить»:
            начнётся новый раунд выбора в прежнем порядке. Замена модели или
            состава требует нового Run.
          </p>
        </div>
      ) : null}
      {nodes.map((node) => (
        <NodeCandidateTable
          key={node.node_id}
          node={node}
          groupName={groups.find((g) => g.id === node.model_group_id)?.name}
          diagnostics={
            node.node_id === diagnosticNodeId &&
            Array.isArray(waiting?.details?.candidates)
              ? waiting.details.candidates
              : []
          }
        />
      ))}
      {!nodes.length ? <p>В графе нет узлов с выбором модели.</p> : null}
    </section>
  )
}

function resource(
  profile: string | null | undefined,
  connection: string | null | undefined,
) {
  return profile
    ? `профиль ${profile}`
    : connection
      ? `подключение ${connection}`
      : 'ресурс не указан'
}

function NodeCandidateTable({
  node,
  groupName,
  diagnostics,
}: {
  node: SelectionNodeEntry
  groupName: string | undefined
  diagnostics: Array<Record<string, unknown>>
}) {
  const visit = node.visit
  const actual = node.actual
  const reasons = new Map(
    diagnostics.map((entry) => [entry.member_index, entry.reason]),
  )
  return (
    <section
      className="group-candidates"
      aria-label={`Кандидаты узла ${node.node_id}`}
    >
      <h5>
        Узел <code>{node.node_id}</code>
        {node.role ? ` · роль ${node.role}` : ''} · {node.kind} ·{' '}
        {node.model_group_id
          ? `группа ${groupName || node.model_group_id}`
          : 'Прямой выбор'}
      </h5>
      {visit ? (
        <p className="hint">
          Последнее посещение #{visit.visit_index} · цикл {visit.cycle_id} ·{' '}
          {visit.status} · повторов {visit.retries} · раунд после исчерпания{' '}
          {visit.selection_round}.
          {visit.current_member_index != null
            ? ` Выбран кандидат #${visit.current_member_index + 1} (${visit.current_model_id}).`
            : ''}
        </p>
      ) : (
        <p className="hint">Узел ещё не выполнялся.</p>
      )}
      {actual ? (
        <p className="actual-selection">
          Исполнитель последней попытки: <code>{actual.model_id}</code> ·
          позиция {actual.member_index + 1} ·{' '}
          {resource(actual.harness_profile_id, actual.provider_connection_id)} ·
          статус {actual.status}
          {actual.outcome ? ` · исход ${actual.outcome}` : ''}
        </p>
      ) : (
        <p className="hint">Вызов исполнителя ещё не зарегистрирован.</p>
      )}
      <div className="candidate-table-scroll">
        <table className="candidate-table">
          <thead>
            <tr>
              <th>Приоритет</th>
              <th>Модель</th>
              <th>Ресурс</th>
              <th>Включён</th>
              <th>Состояние</th>
              <th>Последняя причина</th>
            </tr>
          </thead>
          <tbody>
            {node.candidates?.map((candidate) => (
              <tr key={candidate.member_index} data-state={candidate.state}>
                <td>{candidate.member_index + 1}</td>
                <td>
                  <code>{candidate.model_id}</code>
                </td>
                <td>
                  {resource(
                    candidate.harness_profile_id,
                    candidate.provider_connection_id,
                  )}
                </td>
                <td>{candidate.enabled ? 'да' : 'нет'}</td>
                <td>
                  <span className={`candidate-state state-${candidate.state}`}>
                    {CANDIDATE_STATE_LABELS[candidate.state ?? 'available']}
                  </span>
                </td>
                <td>
                  {reasonLabel(
                    reasons.get(candidate.member_index) ??
                      candidate.last_reason ??
                      '—',
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {visit && !visit.history_complete ? (
        <p className="hint">
          Для старого Run полная история пропусков не сохранена. Исполнитель
          восстановлен из попыток.
        </p>
      ) : null}
      {visit?.history?.length ? (
        <details>
          <summary>
            История выбора узла {node.node_id} · посещение #{visit.visit_index}
          </summary>
          <ol className="candidate-history">
            {visit.history.map((entry, index) => (
              <li key={index}>
                <code>{String(entry.model_id ?? '—')}</code> · позиция{' '}
                {typeof entry.member_index === 'number'
                  ? entry.member_index + 1
                  : '—'}{' '}
                · причина <code>{reasonLabel(entry.reason)}</code> · раунд{' '}
                {String(entry.selection_round ?? 0)}
              </li>
            ))}
          </ol>
        </details>
      ) : null}
    </section>
  )
}
