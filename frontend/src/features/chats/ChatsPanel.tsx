import { Modal } from '../../app/Modal'
import { useState } from 'react'
import { LoaderCircle } from 'lucide-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError } from '../../api/client'
import { chatsApi, describeError, type Chat } from '../../api/projects'
import { planningApi } from '../../api/planning'
import { runsApi } from '../../api/runs'
import { formatDateTime } from '../../app/format'
import { useCsrfToken } from '../../app/session'

interface ChatsPanelProps {
  projectId: string
  selectedId: string | null
  onSelect: (chat: Chat) => void
}

export function ChatsPanel({
  projectId,
  selectedId,
  onSelect,
}: ChatsPanelProps) {
  const client = useQueryClient()
  const [createOpen, setCreateOpen] = useState(false)
  const chats = useQuery({
    queryKey: ['chats', { projectId, includeArchived: false }],
    queryFn: () => chatsApi.list({ projectId, includeArchived: false }),
    enabled: Boolean(projectId),
  })
  const runs = useQuery({
    queryKey: ['runs_summary', { projectId }],
    queryFn: () => runsApi.list({ projectId }),
    enabled: Boolean(projectId),
    refetchInterval: 3000,
  })
  const planning = useQuery({
    queryKey: ['planning-jobs', { projectId }],
    queryFn: () => planningApi.list({ projectId }),
    enabled: Boolean(projectId),
    refetchInterval: 3000,
  })
  const activeChatIds = new Set([
    ...(runs.data ?? [])
      .filter((run) =>
        [
          'queued',
          'running',
          'pause_requested',
          'stop_requested',
          'retry_wait',
          'recovering',
        ].includes(run.state),
      )
      .map((run) => run.chat_id),
    ...(planning.data ?? [])
      .filter((job) => ['drafting', 'merging'].includes(job.state))
      .map((job) => job.chat_id),
  ])

  return (
    <section className="panel" aria-labelledby="chats-heading">
      <header className="panel-header">
        <span className="section-label" id="chats-heading">
          Диалоги
        </span>
        <button
          type="button"
          className="quiet"
          onClick={() => setCreateOpen(true)}
          aria-label="Новый диалог"
          disabled={!projectId}
        >
          + Новый
        </button>
      </header>
      <ChatsList
        chats={chats.data ?? []}
        loading={chats.isLoading}
        error={chats.error ? describeError(chats.error) : null}
        selectedId={selectedId}
        activeChatIds={activeChatIds}
        onSelect={onSelect}
        onRetry={() => void chats.refetch()}
        projectId={projectId}
      />
      {createOpen ? (
        <CreateChatDialog
          projectId={projectId}
          onClose={() => setCreateOpen(false)}
          onCreated={(created) => {
            setCreateOpen(false)
            client.setQueryData<Chat[]>(
              ['chats', { projectId, includeArchived: false }],
              (previous = []) => [
                created,
                ...previous.filter((item) => item.id !== created.id),
              ],
            )
            void client.invalidateQueries({
              queryKey: ['chats', { projectId }],
            })
            onSelect(created)
          }}
        />
      ) : null}
    </section>
  )
}

interface ChatsListProps {
  chats: Chat[]
  loading: boolean
  error: string | null
  selectedId: string | null
  activeChatIds: ReadonlySet<string | null>
  onSelect: (chat: Chat) => void
  onRetry: () => void
  projectId: string
}

function ChatsList({
  chats,
  loading,
  error,
  selectedId,
  activeChatIds,
  onSelect,
  onRetry,
  projectId,
}: ChatsListProps) {
  if (!projectId) {
    return (
      <p className="panel-empty">Выберите проект, чтобы увидеть диалоги.</p>
    )
  }
  if (loading) {
    return (
      <p className="panel-empty" role="status">
        Загружаем диалоги…
      </p>
    )
  }
  if (error) {
    return (
      <div className="panel-empty">
        <p className="error">{error}</p>
        <button type="button" className="quiet" onClick={onRetry}>
          Повторить
        </button>
      </div>
    )
  }
  if (chats.length === 0) {
    return (
      <p className="panel-empty">
        В проекте ещё нет диалогов. Нажмите «+ Новый», чтобы начать.
      </p>
    )
  }
  return (
    <ul className="panel-list" role="listbox" aria-label="Диалоги">
      {chats.map((chat) => {
        const selected = chat.id === selectedId
        return (
          <li key={chat.id}>
            <button
              type="button"
              role="option"
              aria-selected={selected}
              className={`panel-item${selected ? ' selected' : ''}`}
              onClick={() => onSelect(chat)}
            >
              <span className="chat-item-title">
                <strong>{chat.title}</strong>
                {activeChatIds.has(chat.id) ? (
                  <span
                    role="status"
                    aria-label="В диалоге выполняется процесс"
                    title="В диалоге выполняется процесс"
                  >
                    <LoaderCircle
                      className="chat-activity-spinner"
                      size={14}
                      aria-hidden="true"
                    />
                  </span>
                ) : null}
              </span>
              <span className="meta">
                <span>{formatDateTime(chat.updated_at)}</span>
              </span>
            </button>
          </li>
        )
      })}
    </ul>
  )
}

interface CreateChatDialogProps {
  projectId: string
  onClose: () => void
  onCreated: (chat: Chat) => void
}

function CreateChatDialog({
  projectId,
  onClose,
  onCreated,
}: CreateChatDialogProps) {
  const csrf = useCsrfToken()
  const [title, setTitle] = useState('')
  const create = useMutation({
    mutationFn: () => chatsApi.create(projectId, { title: title.trim() }, csrf),
    onSuccess: onCreated,
  })
  const submit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    create.mutate()
  }
  return (
    <Modal
      onClose={onClose}
      busy={create.isPending}
      labelledBy="create-chat-title"
    >
      <form className="dialog" onSubmit={submit}>
        <header>
          <h3 id="create-chat-title">Новый диалог</h3>
          <button
            type="button"
            className="quiet"
            aria-label="Закрыть"
            onClick={onClose}
          >
            ×
          </button>
        </header>
        <label htmlFor="chat-title">Название</label>
        <input
          id="chat-title"
          type="text"
          autoFocus
          required
          maxLength={128}
          value={title}
          onChange={(event) => setTitle(event.target.value)}
        />
        {create.error ? (
          <p className="error" role="alert">
            {renderChatError(create.error)}
          </p>
        ) : null}
        <footer>
          <button type="button" className="quiet" onClick={onClose}>
            Отмена
          </button>
          <button
            type="submit"
            disabled={create.isPending || !title.trim() || !csrf}
          >
            {create.isPending ? 'Создаём…' : 'Создать'}
          </button>
        </footer>
      </form>
    </Modal>
  )
}

function renderChatError(error: unknown): string {
  if (error instanceof ApiError) return error.body.message
  return describeError(error)
}
