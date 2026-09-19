import { CircleAlert, LoaderCircle } from 'lucide-react'
import type { ActivityStatus } from '../api/activity'

export function ActivityIndicators({
  activity,
  scope,
}: {
  activity?: ActivityStatus
  scope: 'project' | 'chat'
}) {
  if (!activity?.running && !activity?.attention) return null
  const location = scope === 'project' ? 'В проекте' : 'В диалоге'
  const runningLabel = `${location} выполняется процесс`
  const attentionLabel = `${location} требуется внимание: ошибка или запрос`
  return (
    <span className="activity-indicators">
      {activity.running ? (
        <span role="status" aria-label={runningLabel} title={runningLabel}>
          <LoaderCircle
            className="chat-activity-spinner"
            size={14}
            aria-hidden="true"
          />
        </span>
      ) : null}
      {activity.attention ? (
        <span
          className="activity-attention"
          role="status"
          aria-label={attentionLabel}
          title={attentionLabel}
        >
          <CircleAlert size={16} aria-hidden="true" />
        </span>
      ) : null}
    </span>
  )
}
