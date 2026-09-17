import { isAgentSelection, isLLMSelection } from './council_members'
import type { PlanningMemberSpec } from '../../api/planning'
import type {
  HarnessProfile,
  ModelGroup,
  ProviderConnection,
} from '../../api/settings'

export interface CouncilResources {
  providers: ProviderConnection[]
  harnesses: HarnessProfile[]
  groups: ModelGroup[]
}

type ResourceKind = 'llm' | 'agent'

export function CouncilMemberEditor({
  spec,
  title,
  onChange,
  resources,
  locked = false,
}: {
  spec: PlanningMemberSpec
  title: string
  onChange: (next: PlanningMemberSpec) => void
  resources: CouncilResources | undefined
  locked?: boolean
}) {
  const directKind: ResourceKind = isAgentSelection(spec.selection)
    ? 'agent'
    : 'llm'
  const showHarnesses = () =>
    (resources?.harnesses ?? [])
      .filter((h) => !h.archived && h.harness_kind !== undefined)
      .map((h) => ({
        id: h.id,
        name: `${h.name} (${h.harness_kind})`,
      }))
  return (
    <fieldset className="council-member" disabled={locked}>
      <legend>{title}</legend>
      <label>
        Тип выбора
        <select
          value={spec.selection.kind}
          onChange={(e) => {
            const kind = e.target.value
            if (kind === 'group') {
              onChange({
                role: spec.role,
                selection: { kind: 'group', group_id: '' },
              })
              return
            }
            if (directKind === 'agent') {
              onChange({
                role: spec.role,
                selection: {
                  kind: 'direct',
                  model_id: '',
                  harness_profile_id: '',
                },
              })
            } else {
              onChange({
                role: spec.role,
                selection: {
                  kind: 'direct',
                  model_id: '',
                  provider_connection_id: '',
                },
              })
            }
          }}
        >
          <option value="direct">Прямая модель</option>
          <option value="group">Группа</option>
        </select>
      </label>
      {spec.selection.kind === 'direct' ? (
        <label>
          Подтип
          <select
            value={directKind}
            onChange={(e) => {
              const next: ResourceKind = e.target.value as ResourceKind
              if (next === 'agent') {
                onChange({
                  ...spec,
                  selection: {
                    kind: 'direct',
                    model_id:
                      spec.selection.kind === 'direct'
                        ? spec.selection.model_id
                        : '',
                    harness_profile_id: '',
                  },
                })
              } else {
                onChange({
                  ...spec,
                  selection: {
                    kind: 'direct',
                    model_id:
                      spec.selection.kind === 'direct'
                        ? spec.selection.model_id
                        : '',
                    provider_connection_id: '',
                  },
                })
              }
            }}
          >
            <option value="llm">LLM</option>
            <option value="agent">Агент (Codex/OpenCode)</option>
          </select>
        </label>
      ) : null}
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
            {resources?.groups
              .filter((g) => !g.archived)
              .map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name} ({g.kind === 'agent' ? 'агент' : 'LLM'})
                </option>
              ))}
          </select>
        </label>
      ) : directKind === 'agent' ? (
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
            Профиль harness
            <select
              value={
                isAgentSelection(spec.selection)
                  ? spec.selection.harness_profile_id
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
                    harness_profile_id: e.target.value,
                  },
                })
              }
            >
              <option value="">Выберите профиль</option>
              {showHarnesses().map((h) => (
                <option key={h.id} value={h.id}>
                  {h.name}
                </option>
              ))}
            </select>
          </label>
          <p className="hint">
            На Windows поддерживаются Codex в режиме чтения и OpenCode без
            инструментов. Каждый участник получает отдельную сессию.
          </p>
        </>
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
                isLLMSelection(spec.selection)
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
              {resources?.providers
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
}
