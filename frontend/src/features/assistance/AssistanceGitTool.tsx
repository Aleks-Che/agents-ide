import { useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  assistanceApi,
  type AssistanceTarget,
  type AssistanceTool,
  type AssistanceToolResult,
} from '../../api/assistance'
import { runsApi, type GitChangesReview } from '../../api/runs'
import {
  GitChangesResolution,
  type GitChangesResolutionPayload,
} from '../runs/GitChangesResolution'

export type AssistanceToolProps = {
  tool: AssistanceTool
  target: AssistanceTarget
  csrf?: string
  disabled: boolean
  onBusy: (busy: boolean) => void
  onResult: (result: AssistanceToolResult) => void
}

export function AssistanceGitTool({
  tool,
  target,
  csrf,
  disabled,
  onBusy,
  onResult,
}: AssistanceToolProps) {
  const [opened, setOpened] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()
  const pending = useRef(false)
  const isHead = tool.name === 'accept_git_head_and_resume'
  const run = useQuery({
    queryKey: ['assistance-tool-run', tool.run_id],
    queryFn: () => runsApi.get(tool.run_id),
    enabled: opened,
    refetchOnWindowFocus: false,
    refetchInterval: opened && !busy ? 10000 : false,
  })

  async function execute(
    payload: GitChangesResolutionPayload,
    review: GitChangesReview,
  ) {
    if (!csrf || pending.current || disabled) return
    if (review.kind !== (isHead ? 'head' : 'protected_files')) {
      setError(
        'Тип проблемы изменился. Обновите диагностику перед выполнением.',
      )
      return
    }
    pending.current = true
    setBusy(true)
    setError(undefined)
    onBusy(true)
    try {
      const result = await assistanceApi.executeTool(
        {
          tool: tool.name,
          target,
          run_id: tool.run_id,
          comparison_id: review.comparison_id,
          expected_state_version: review.state_version,
          paths: payload.git_changes.paths,
          acknowledge_risk: true,
          confirm_resume: true,
        },
        csrf,
      )
      setOpened(false)
      onResult(result)
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : 'Не удалось выполнить инструмент.',
      )
      // Keep the concrete comparison visible. The user can refresh it before
      // another confirmation; an uncertain response can replay the same token.
    } finally {
      pending.current = false
      setBusy(false)
      onBusy(false)
    }
  }

  return (
    <section
      className="assistance-tool"
      aria-label={`Инструмент помощника: ${tool.title}`}
    >
      <strong>Решение для «{tool.title}»</strong>
      <p>{tool.description}</p>
      {!opened && (
        <button
          type="button"
          disabled={disabled || !csrf}
          onClick={() => {
            setOpened(true)
            setError(undefined)
            void run.refetch()
          }}
        >
          Решить через помощника
        </button>
      )}
      {opened && run.isPending && <p role="status">Готовим инструмент…</p>}
      {opened && run.error && (
        <p className="error" role="alert">
          {run.error.message}
        </p>
      )}
      {opened && run.data && (
        <GitChangesResolution
          run={run.data}
          busy={busy || disabled}
          resumeAfterAcceptance
          onSubmit={(payload, review) => void execute(payload, review)}
          onClose={() => setOpened(false)}
        />
      )}
      {busy && (
        <p role="status">
          {isHead
            ? 'Проверяем, принимаем HEAD и продолжаем запуск…'
            : 'Проверяем, принимаем выбранные изменения файлов и продолжаем запуск…'}
        </p>
      )}
      {error && (
        <p className="error" role="alert">
          {error}{' '}
          <button
            type="button"
            className="quiet"
            disabled={busy}
            onClick={() => void run.refetch()}
          >
            Обновить состояние инструмента
          </button>
        </p>
      )}
    </section>
  )
}
