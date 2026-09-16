import type {
  EventEnvelope,
  HistoryCategory,
  RunObservation,
} from '../../api/runs'

export const EVENT_WINDOW = 400
export const EVENT_BYTES = 512 * 1024
export const categories: Record<HistoryCategory, string> = {
  all: 'Все события',
  steps: 'Шаги и попытки',
  messages: 'Сообщения',
  tools: 'Инструменты и разрешения',
  models: 'Модели и переключения',
  commands: 'Команды и Git',
  checks: 'Проверки и артефакты',
}
const prefixes: Record<HistoryCategory, string[]> = {
  all: [''],
  steps: ['run.', 'node.', 'transition.', 'attempt.'],
  messages: ['agent.message_delta', 'attempt.text_delta'],
  tools: ['agent.tool_call', 'agent.permission_'],
  models: [
    'model_group.',
    'attempt.started',
    'attempt.finished',
    'attempt.retry_',
  ],
  commands: ['control.', 'command.', 'git.'],
  checks: ['condition.', 'evidence.', 'plan.', 'artifact.'],
}

export function matchesEvent(
  event: EventEnvelope,
  category: HistoryCategory,
  nodeId: string,
) {
  return (
    (!nodeId || event.node_id === nodeId) &&
    prefixes[category].some((p) => event.type.startsWith(p))
  )
}

// Both count and bytes are bounded. Evicted events remain in the server history.
export function mergeEvents(
  previous: EventEnvelope[],
  incoming: EventEnvelope[],
) {
  const rows = [
    ...new Map([...previous, ...incoming].map((e) => [e.sequence, e])).values(),
  ].sort((a, b) => b.sequence - a.sequence)
  let bytes = 0
  return rows.slice(0, EVENT_WINDOW).filter((event) => {
    bytes += JSON.stringify(event).length * 2
    return bytes <= EVENT_BYTES
  })
}

export function selectedEdge(
  edge: NonNullable<RunObservation['edges']>[number],
  transition: RunObservation['last_transition'],
) {
  if (!transition) return false
  return transition.edge_id
    ? edge.id === transition.edge_id
    : edge.source === transition.source_node_id &&
        edge.target === transition.target &&
        (!edge.when || edge.when === transition.reason)
}

export function duration(start?: number | null, finish?: number | null) {
  if (start == null) return '—'
  const seconds = Math.max(0, (finish ?? Date.now() / 1000) - start)
  return seconds < 60
    ? `${seconds.toFixed(1)} с`
    : `${Math.floor(seconds / 60)} мин ${Math.floor(seconds % 60)} с`
}

export function eventPreview(event: EventEnvelope) {
  const p = event.payload
  return [
    p.text,
    p.delta,
    p.message,
    p.tool,
    p.name,
    p.model_id,
    p.reason,
    p.status,
    p.result,
    p.decision,
    p.target,
  ]
    .filter((value) => value !== undefined && value !== null)
    .map((value) => (typeof value === 'string' ? value : JSON.stringify(value)))
    .join(' · ')
    .slice(0, 240)
}

export const stateDescriptions: Record<string, string> = {
  queued: 'В очереди исполнителя',
  running: 'Выполняется',
  pause_requested: 'Пауза запрошена: текущий шаг ещё выполняется',
  stop_requested: 'Остановка запрошена: ожидаем завершения процессов',
  paused: 'На паузе между шагами',
  stopped: 'Остановлен; можно продолжить',
  retry_wait: 'Ожидание повторной попытки',
  recovering: 'Исполнитель сверяет состояние после перезапуска',
  waiting_input: 'Нужно решение пользователя',
  completed: 'Завершён',
  failed: 'Ошибка выполнения',
  cancelled: 'Отменён',
}

export const waitingDescriptions: Record<string, string> = {
  limit_exceeded:
    'Достигнут лимит. Укажите новый предел, сохраните решение и продолжите.',
  permission_required:
    'Операции не хватило разрешения. Проверьте указанный запрос и настройки исполнителя перед новой попыткой.',
  unknown_external_result:
    'Результат внешней операции неизвестен. Проверьте последствия и приложите доказательства перед повтором или принятием результата.',
  invalid_response_format:
    'Ответ не соответствует схеме. Исправьте причину и разрешите новую попытку.',
  model_group_exhausted:
    'Все кандидаты группы исчерпаны. Восстановите доступ к закреплённым ресурсам и нажмите «Продолжить».',
  workspace_conflict:
    'Рабочий каталог занят или изменился. Проверьте диагностику и устраните конфликт.',
  configuration_invalid:
    'Не хватает корректных настроек. Подробности указаны ниже.',
  no_progress:
    'Повторные шаги не дали прогресса. Проверьте отчёты и предоставьте данные решения.',
}
