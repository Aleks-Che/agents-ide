import type { RunRecord, RunCommand } from '../../api/runs'

export function pendingGitChanges(run?: RunRecord): boolean {
  const status = run?.waiting_reason?.resolution_schema?.git_changes as
    { accepted?: boolean } | undefined
  return (
    !!run &&
    ['waiting_input', 'paused', 'stopped'].includes(run.state) &&
    status?.accepted === true
  )
}

export function pendingAgentRecovery(run?: RunRecord): boolean {
  if (!run || !['waiting_input', 'paused'].includes(run.state)) return false
  const recovery = run.waiting_reason?.resolution_schema?.agent_recovery as
    { attempt_id?: string } | undefined
  const attempt =
    recovery?.attempt_id ?? run.waiting_reason?.details?.attempt_id
  const pending = run.runtime?.pending_agent_recovery as
    { attempt_id?: string; execution_id?: string; action?: string } | undefined
  return (
    !!pending?.attempt_id &&
    pending.attempt_id === attempt &&
    ['continue_session', 'next_candidate'].includes(String(pending.action))
  )
}

export function allowedCommands(
  run: RunRecord,
  target?: Record<string, unknown>,
): RunCommand['command_type'][] {
  const states: Record<string, RunCommand['command_type'][]> = {
    queued: ['pause', 'stop', 'cancel'],
    running: ['pause', 'stop', 'cancel'],
    retry_wait: ['pause', 'stop', 'cancel'],
    pause_requested: ['pause', 'stop', 'cancel'],
    stop_requested: ['stop', 'cancel'],
    recovering: ['stop', 'cancel'],
    paused: ['stop', 'resume', 'cancel', 'resolve'],
    stopped: ['resume', 'cancel', 'resolve'],
    waiting_input: ['pause', 'stop', 'resume', 'cancel', 'resolve'],
  }
  return (states[run.state] ?? []).filter((command) => {
    if (
      run.waiting_reason &&
      !run.waiting_reason.allowed_actions.some((action) => action === command)
    )
      return false
    return (
      command !== 'resolve' ||
      run.state === 'waiting_input' ||
      (Array.isArray(target?.blockers) && target.blockers.length > 0)
    )
  })
}

export function resolutionPayload(
  run: RunRecord,
  text: string,
  action: string,
  processing?: Record<string, unknown>,
): Record<string, unknown> {
  const reason =
    run.waiting_reason ??
    (run.runtime?.waiting_reason as RunRecord['waiting_reason'])
  const code = reason?.code ?? run.runtime?.waiting_code
  if (['continue_session', 'next_candidate'].includes(action)) {
    const recovery = reason?.resolution_schema?.agent_recovery as
      { attempt_id?: string; actions?: string[] } | undefined
    if (!recovery?.attempt_id || !recovery.actions?.includes(action))
      throw new Error('Продолжение недоступно для этой попытки.')
    return { agent_recovery: { attempt_id: recovery.attempt_id, action } }
  }
  if (code === 'invalid_response_format' && action === 'reprocess') {
    if (
      !processing ||
      !Object.values(processing).some((value) => value === true)
    )
      throw new Error('Выберите обработку JSON-ответа.')
    return { json_processing: processing }
  }
  if (code === 'limit_exceeded') {
    const value = Number(text)
    const limit = reason?.details?.limit
    if (typeof limit !== 'string' || !Number.isSafeInteger(value) || value <= 0)
      throw new Error('Нужен известный лимит и положительное целое число.')
    return { limit_overrides: { [limit]: value } }
  }
  if (code === 'permission_required' || code === 'invalid_response_format') {
    if (action !== 'retry') throw new Error('Подтвердите новую попытку.')
    return { retry: true }
  }
  if (code === 'unknown_external_result') {
    const attempt =
      reason?.details?.attempt_id ?? run.runtime?.current_attempt_id
    if (
      !attempt ||
      !text.trim() ||
      !['accept_result', 'retry_authorized'].includes(action)
    )
      throw new Error('Укажите попытку, действие и доказательства сверки.')
    return {
      reconciliation: { attempt_id: attempt, action, evidence: text.trim() },
    }
  }
  const data: unknown = JSON.parse(text)
  if (
    data === null ||
    typeof data !== 'object' ||
    Array.isArray(data) ||
    !Object.keys(data).length
  )
    throw new Error('Нужен непустой JSON-объект с данными решения.')
  return { data }
}
