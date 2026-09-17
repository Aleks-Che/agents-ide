export type HarnessExecutionOptions = {
  permission_mode?: string
  auto_approve?: boolean
  approval_policy?: string
}

export function HarnessExecutionSettings({
  kind,
  value,
  onChange,
  disabled = false,
}: {
  kind: 'codex' | 'opencode'
  value: HarnessExecutionOptions
  onChange: (value: HarnessExecutionOptions) => void
  disabled?: boolean
}) {
  const modes =
    kind === 'codex'
      ? [
          ['read_only', 'Только чтение'],
          ['workspace_write', 'Изменение файлов проекта'],
          ['full_access', 'Полный доступ к компьютеру'],
        ]
      : [
          ['native', 'Как в OpenCode — инструменты и разрешения'],
          ['no_tools', 'Без инструментов — только переданный контекст'],
        ]
  const auto =
    value.auto_approve ??
    (kind === 'codex' && (value.approval_policy ?? 'never') === 'never')
  return (
    <fieldset className="harness-execution-settings" disabled={disabled}>
      <legend>{kind === 'codex' ? 'Codex' : 'OpenCode'} · запуск агента</legend>
      <label>
        Режим доступа
        <select
          value={value.permission_mode ?? modes[0][0]}
          onChange={(event) =>
            onChange({ ...value, permission_mode: event.target.value })
          }
        >
          {modes.map(([id, label]) => (
            <option key={id} value={id}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <label className="checkbox-row">
        <input
          type="checkbox"
          checked={auto}
          disabled={kind === 'opencode' && value.permission_mode === 'no_tools'}
          onChange={(event) =>
            onChange({ ...value, auto_approve: event.target.checked })
          }
        />
        Автоматически одобрять действия
      </label>
      <p className="hint">
        {kind === 'codex'
          ? 'При автоодобрении агент работает без запросов подтверждения в выбранном режиме доступа. В режиме проекта запись ограничена проектом, сеть отключена.'
          : 'Автоодобрение подтверждает запросы OpenCode. Явные запреты из его настроек сохраняются.'}
      </p>
      {kind === 'codex' && value.permission_mode === 'full_access' ? (
        <p className="hint">
          Агент сможет выполнять команды, обращаться к сети и изменять файлы за
          пределами проекта.
        </p>
      ) : null}
    </fieldset>
  )
}

export function NodeHarnessSettings({
  value,
  onChange,
}: {
  value: Record<string, HarnessExecutionOptions>
  onChange: (value: Record<string, HarnessExecutionOptions>) => void
}) {
  return (
    <section>
      <strong>Настройки harness для этого узла</strong>
      <p className="hint">
        По умолчанию используются общие настройки. Для группы каждый тип harness
        настраивается отдельно.
      </p>
      {(['codex', 'opencode'] as const).map((kind) => (
        <div key={kind}>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={!!value[kind]}
              onChange={(event) => {
                const next = { ...value }
                if (event.target.checked)
                  next[kind] = {
                    permission_mode:
                      kind === 'codex' ? 'workspace_write' : 'native',
                    auto_approve: true,
                  }
                else delete next[kind]
                onChange(next)
              }}
            />
            Свои настройки {kind === 'codex' ? 'Codex' : 'OpenCode'}
          </label>
          {value[kind] ? (
            <HarnessExecutionSettings
              kind={kind}
              value={value[kind]}
              onChange={(options) => onChange({ ...value, [kind]: options })}
            />
          ) : null}
        </div>
      ))}
    </section>
  )
}
