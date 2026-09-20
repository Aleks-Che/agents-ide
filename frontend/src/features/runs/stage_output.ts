import type { EventEnvelope } from '../../api/runs'
import { eventPreview, toolProgress } from './observation'

interface StageOutputEntry {
  key: number
  occurredAt: number
  text: string
  role: string
  tool?: { scope: string; count: number }
}

export function stageOutput(all: EventEnvelope[]): StageOutputEntry[] {
  const output: StageOutputEntry[] = []
  const tools = new Map<string, StageOutputEntry>()
  for (const event of all) {
    if (
      event.type === 'attempt.text_delta' ||
      event.type === 'agent.message_delta'
    ) {
      const text = String(event.payload.text ?? event.payload.delta ?? '')
      const last = output.at(-1)
      if (last?.role === `assistant:${event.step_attempt_id}`) last.text += text
      else
        output.push({
          key: event.sequence,
          occurredAt: event.occurred_at,
          text,
          role: `assistant:${event.step_attempt_id}`,
        })
    } else if (event.type === 'agent.tool_call') {
      const tool = toolProgress(event)
      const previous = tools.get(tool.id)
      if (previous) previous.text = tool.text
      else {
        const entry: StageOutputEntry = {
          key: event.sequence,
          occurredAt: event.occurred_at,
          text: tool.text,
          role: 'system',
          tool: {
            scope: JSON.stringify([
              event.node_id,
              event.step_execution_id,
              event.step_attempt_id,
            ]),
            count: 1,
          },
        }
        tools.set(tool.id, entry)
        output.push(entry)
      }
    } else if (event.type === 'agent.user_message') {
      const delivery = all.find(
        (next) =>
          next.type === 'agent.user_message_status' &&
          next.command_id === event.command_id,
      )
      const status = delivery?.payload.delivery
      output.push({
        key: event.sequence,
        occurredAt: event.occurred_at,
        text: `${String(event.payload.text ?? '')}\n${status === 'delivered' ? 'Передано агенту' : status === 'failed' ? 'Не удалось подтвердить доставку' : 'Ожидает передачи агенту'}`,
        role: 'user',
      })
    } else if (
      [
        'attempt.started',
        'attempt.finished',
        'command.finished',
        'condition.evaluated',
        'git.commit_created',
        'git.no_changes',
      ].includes(event.type)
    ) {
      output.push({
        key: event.sequence,
        occurredAt: event.occurred_at,
        text:
          event.type === 'attempt.finished' &&
          event.payload.status === 'interrupted'
            ? 'Попытка прервана; остановка подтверждена'
            : `${({ 'attempt.started': 'Попытка начата', 'attempt.finished': 'Попытка завершена', 'command.finished': 'Команда завершена', 'condition.evaluated': 'Условие проверено', 'git.commit_created': 'Коммит создан', 'git.no_changes': 'Нет изменений для коммита' } as Record<string, string>)[event.type]} ${eventPreview(event)}`,
        role: 'system',
      })
    }
  }

  // Resolve call updates first, then compact identical neighbouring calls.
  // Updates to an existing call never increase the displayed call count.
  const grouped: StageOutputEntry[] = []
  for (const entry of output) {
    const previous = grouped.at(-1)
    if (
      entry.tool &&
      previous?.tool &&
      entry.tool.scope === previous.tool.scope &&
      entry.text === previous.text
    )
      previous.tool.count += entry.tool.count
    else grouped.push(entry)
  }
  return grouped
}
