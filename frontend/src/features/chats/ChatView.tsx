import { useEffect, useRef, useState } from 'react'
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import { ApiError } from '../../api/client'
import { bindingsApi } from '../../api/bindings'
import {
  chatsApi,
  describeError,
  messagesApi,
  projectsApi,
  type Chat,
  type Message,
  type Project,
} from '../../api/projects'
import { ChatRunsList, LaunchRunDialog } from '../runs/LaunchRunDialog'
import { RunScreen } from '../runs/RunsPanel'
import { ChatRunProgress } from '../runs/ChatRunProgress'
import { runsApi } from '../../api/runs'
import { formatDateTime } from '../../app/format'
import { useCsrfToken } from '../../app/session'
import { CouncilPanel, CouncilReviewer } from '../planning/CouncilPanel'
import {
  planningApi,
  type PlanningJobView,
  type PlanningSource,
} from '../../api/planning'

interface ChatViewProps {
  projectId: string | null
  chat: Chat | null
  draftText: string
  onDraftChange: (text: string) => void
  onArchived: (id: string) => void
  onOpenTemplates: (templateId?: string) => void
}

export function ChatView({
  projectId,
  chat,
  onArchived,
  draftText,
  onDraftChange,
  onOpenTemplates,
}: ChatViewProps) {
  const client = useQueryClient()
  const csrf = useCsrfToken()
  const [launchOpen, setLaunchOpen] = useState(false)
  const [openRunId, setOpenRunId] = useState<string | null>(null)
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [lastLaunched, setLastLaunched] = useState<string | null>(null)
  const [councilOpen, setCouncilOpen] = useState(false)
  const [councilJobId, setCouncilJobId] = useState<string | null>(null)
  const [planningSource, setPlanningSource] = useState<PlanningSource | null>(
    null,
  )
  const councilJobs = useQuery({
    queryKey: ['planning-jobs', chat?.id],
    queryFn: () =>
      planningApi.list({ chatId: chat!.id, includeCompleted: true }),
    enabled: Boolean(chat),
    refetchInterval: 5000,
  })
  const runs = useQuery({
    queryKey: [
      'runs_for_chat',
      { projectId: projectId ?? '', chatId: chat?.id ?? '' },
    ],
    queryFn: () => runsApi.list({ projectId: projectId!, chatId: chat!.id }),
    enabled: Boolean(projectId && chat),
    refetchInterval: 3000,
  })
  const selectedRun =
    runs.data?.find((run) => run.id === selectedRunId) ??
    runs.data?.find(
      (run) => !['completed', 'failed', 'cancelled'].includes(run.state),
    ) ??
    runs.data?.[0]

  const messages = useInfiniteQuery({
    queryKey: ['messages', { chatId: chat?.id ?? '' }],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      messagesApi.list({
        chatId: chat!.id,
        limit: 200,
        latest: true,
        beforeId: pageParam,
      }),
    getNextPageParam: (page) => (page.length === 200 ? page[0].id : undefined),
    enabled: Boolean(chat),
  })

  const project = useQuery({
    queryKey: ['project', { projectId: projectId ?? '' }],
    queryFn: () => projectsApi.list({ includeArchived: true }),
    enabled: Boolean(projectId),
    select: (list) => list.find((item) => item.id === projectId) ?? null,
  })

  const bindings = useQuery({
    queryKey: ['bindings', { projectId: projectId ?? '' }],
    queryFn: () => bindingsApi.list({ projectId: projectId ?? undefined }),
    enabled: Boolean(projectId),
  })

  const archiveChat = useMutation({
    mutationFn: () =>
      chatsApi.archive(
        chat!.id,
        { archive: true, expected_version: chat!.version },
        csrf,
      ),
    onSuccess: (archived) => {
      client.setQueryData<Chat[]>(
        ['chats', { projectId, includeArchived: false }],
        (previous = []) => previous.filter((item) => item.id !== archived.id),
      )
      void client.invalidateQueries({
        queryKey: ['chats', { projectId, includeArchived: false }],
      })
      onArchived(archived.id)
    },
  })

  const createMessage = useMutation({
    mutationFn: () =>
      messagesApi.create(
        chat!.id,
        { content: draftText.trim(), role: 'user' },
        csrf,
      ),
    onSuccess: () => {
      onDraftChange('')
      void client.invalidateQueries({
        queryKey: ['messages', { chatId: chat!.id }],
      })
    },
  })

  const projectValue: Project | null = project.data ?? null

  const councilJob = useQuery({
    queryKey: ['planning-job', councilJobId],
    queryFn: () => planningApi.get(councilJobId!),
    enabled: Boolean(councilJobId),
    refetchInterval: (query) => {
      const data = query.state.data as PlanningJobView | undefined
      if (!data) return false
      if (
        data.state === 'confirmed' ||
        data.state === 'cancelled' ||
        data.state === 'failed'
      ) {
        return false
      }
      return 3000
    },
  })

  if (!chat) {
    return (
      <section className="main-pane">
        <header className="main-header">
          <h2>Диалог не выбран</h2>
          <span className="muted">
            Создайте или выберите диалог, чтобы начать работу.
          </span>
        </header>
      </section>
    )
  }

  return (
    <section className="main-pane" aria-labelledby="chat-title">
      <header className="main-header">
        <div>
          <span className="eyebrow">Диалог</span>
          <h2 id="chat-title">{chat.title}</h2>
          <span className="muted">
            Создан {formatDateTime(chat.created_at)} · обновлён{' '}
            {formatDateTime(chat.updated_at)}
          </span>
        </div>
        <div className="actions">
          <button
            type="button"
            className="quiet primary"
            onClick={() => setLaunchOpen(true)}
            disabled={
              !csrf ||
              archiveChat.isPending ||
              createMessage.isPending ||
              !projectValue ||
              projectValue.archived ||
              chat.archived
            }
            aria-label="Запустить шаблон"
            title="START — запустить шаблон"
          >
            <span aria-hidden="true">▶</span> Запустить шаблон
          </button>
          <button
            type="button"
            className="quiet"
            onClick={() => setCouncilOpen(true)}
            disabled={
              !csrf ||
              archiveChat.isPending ||
              createMessage.isPending ||
              !projectValue ||
              projectValue.archived ||
              chat.archived
            }
            aria-label="Составить план несколькими моделями"
          >
            Совет моделей
          </button>
          <button
            type="button"
            className="quiet danger"
            onClick={() => archiveChat.mutate()}
            disabled={archiveChat.isPending || createMessage.isPending || !csrf}
            aria-label="Архивировать диалог"
          >
            {archiveChat.isPending ? 'Архивируем…' : 'Архивировать'}
          </button>
        </div>
      </header>
      {project.error ? (
        <p role="alert" className="error">
          Не удалось загрузить проект: {describeError(project.error)}{' '}
          <button type="button" onClick={() => void project.refetch()}>
            Повторить загрузку проекта
          </button>
        </p>
      ) : null}
      {lastLaunched ? (
        <p className="hint" role="status">
          Запуск создан. Этапы и сообщения агента отображаются в этом диалоге.
        </p>
      ) : null}
      {archiveChat.error ? (
        <p className="error" role="alert">
          {renderMessageError(archiveChat.error)}
        </p>
      ) : null}
      {messages.hasNextPage ? (
        <button
          type="button"
          className="quiet"
          disabled={messages.isFetchingNextPage}
          onClick={() => void messages.fetchNextPage()}
        >
          Загрузить более ранние сообщения
        </button>
      ) : null}
      <MessagesList
        messages={
          messages.data ? [...messages.data.pages].reverse().flat() : []
        }
        loading={messages.isLoading}
        error={messages.error ? describeError(messages.error) : null}
      />
      {selectedRun ? (
        <ChatRunProgress
          key={selectedRun.id}
          runId={selectedRun.id}
          onDetails={() => setOpenRunId(selectedRun.id)}
          onNewRun={() => setLaunchOpen(true)}
          onRestarted={(id) => setSelectedRunId(id)}
        />
      ) : null}
      <form
        className="composer"
        onSubmit={(event) => {
          event.preventDefault()
          createMessage.mutate()
        }}
      >
        <label htmlFor="message-input" className="muted">
          Новое сообщение
        </label>
        <textarea
          id="message-input"
          value={draftText}
          onChange={(event) => onDraftChange(event.target.value)}
          placeholder="Опишите задачу или отправьте пояснение…"
          rows={4}
          disabled={!csrf || createMessage.isPending || archiveChat.isPending}
        />
        {createMessage.error ? (
          <p className="error" role="alert">
            {renderMessageError(createMessage.error)}
          </p>
        ) : null}
        <footer>
          <span className="muted">
            Это заметка диалога. Для ответа работающему агенту используйте поле
            внутри текущего этапа.
          </span>
          <button
            type="submit"
            disabled={
              createMessage.isPending ||
              archiveChat.isPending ||
              !draftText.trim() ||
              !csrf
            }
          >
            {createMessage.isPending ? 'Отправляем…' : 'Отправить'}
          </button>
        </footer>
      </form>
      <section className="chat-runs" aria-labelledby="chat-runs-title">
        <header>
          <h3 id="chat-runs-title">Запуски этого диалога</h3>
          <span className="muted">
            История запусков, созданных для выбранного диалога.
          </span>
        </header>
        <ChatRunsList
          projectId={projectId ?? ''}
          chatId={chat.id}
          onSelectRun={(id) => {
            setSelectedRunId(id)
            setOpenRunId(id)
          }}
        />
      </section>
      {launchOpen && projectValue ? (
        <LaunchRunDialog
          planningSource={planningSource}
          project={projectValue}
          chat={chat}
          draft={draftText}
          bindings={bindings.data ?? []}
          bindingsLoading={bindings.isLoading}
          bindingsError={bindings.error}
          onRetryBindings={() => void bindings.refetch()}
          onOpenTemplates={onOpenTemplates}
          onClose={() => {
            setLaunchOpen(false)
            setPlanningSource(null)
          }}
          onLaunched={(run) => {
            setLaunchOpen(false)
            setPlanningSource(null)
            setLastLaunched(run.id)
            setSelectedRunId(run.id)
            void client.invalidateQueries({ queryKey: ['runs_for_chat'] })
          }}
        />
      ) : null}
      {councilJobs.error ? (
        <p role="alert">
          Не удалось загрузить планы.{' '}
          <button onClick={() => void councilJobs.refetch()}>
            Повторить загрузку планов
          </button>
        </p>
      ) : null}
      {councilJobs.data?.length ? (
        <section className="chat-runs" aria-label="Планы этого диалога">
          <h3>Планы этого диалога</h3>
          {councilJobs.data.map((job) => (
            <button key={job.id} onClick={() => setCouncilJobId(job.id)}>
              {job.task_text.slice(0, 70)} · {job.state}
            </button>
          ))}
        </section>
      ) : null}
      {councilJob.error ? (
        <p role="alert">
          Не удалось загрузить план.{' '}
          <button onClick={() => void councilJob.refetch()}>
            Повторить загрузку плана
          </button>
        </p>
      ) : null}
      {councilOpen && projectValue ? (
        <CouncilPanel
          projectId={projectValue.id}
          chatId={chat.id}
          open={councilOpen}
          onClose={() => setCouncilOpen(false)}
          onLaunched={(job) => {
            setCouncilJobId(job.id)
            setCouncilOpen(false)
            void client.invalidateQueries({ queryKey: ['planning-jobs'] })
          }}
        />
      ) : null}
      {councilJobId && councilJob.data ? (
        <CouncilReviewer
          job={councilJob.data}
          onClose={() => setCouncilJobId(null)}
          onUse={(source) => {
            setPlanningSource(source)
            setCouncilJobId(null)
            setLaunchOpen(true)
          }}
        />
      ) : null}
      {openRunId ? (
        <RunScreen
          key={openRunId}
          runId={openRunId}
          onClose={() => setOpenRunId(null)}
        />
      ) : null}
    </section>
  )
}

interface MessagesListProps {
  messages: Message[]
  loading: boolean
  error: string | null
}

function MessagesList({ messages, loading, error }: MessagesListProps) {
  const listRef = useRef<HTMLOListElement>(null)
  const lastId = messages.at(-1)?.id
  useEffect(() => {
    if (listRef.current)
      listRef.current.scrollTop = listRef.current.scrollHeight
  }, [lastId])
  if (loading) {
    return (
      <p className="panel-empty" role="status">
        Загружаем сообщения…
      </p>
    )
  }
  if (error) {
    return <p className="error">{error}</p>
  }
  if (messages.length === 0) {
    return (
      <p className="panel-empty">
        Сообщений ещё нет. Отправьте первое пояснение или задачу.
      </p>
    )
  }
  return (
    <ol ref={listRef} className="messages" aria-label="Сообщения диалога">
      {messages.map((message) => (
        <li key={message.id} className={`message role-${message.role}`}>
          <header>
            <span className="role">{labelRole(message.role)}</span>
            <span className="muted">{formatDateTime(message.created_at)}</span>
          </header>
          <p>{message.content}</p>
        </li>
      ))}
    </ol>
  )
}

function labelRole(role: string): string {
  switch (role) {
    case 'user':
      return 'Вы'
    case 'assistant':
      return 'Ассистент'
    case 'system':
      return 'Система'
    case 'note':
      return 'Заметка'
    default:
      return role
  }
}

function renderMessageError(error: unknown): string {
  if (error instanceof ApiError) return error.body.message
  return describeError(error)
}
