import {
  useInfiniteQuery,
  useMutation,
  useQueryClient,
} from '@tanstack/react-query'
import { ApiError } from '../../api/client'
import {
  chatsApi,
  describeError,
  messagesApi,
  type Chat,
  type Message,
} from '../../api/projects'
import { formatDateTime } from '../../app/format'
import { useCsrfToken } from '../../app/session'

interface ChatViewProps {
  projectId: string | null
  chat: Chat | null
  draftText: string
  onDraftChange: (text: string) => void
  onArchived: (id: string) => void
}

export function ChatView({
  projectId,
  chat,
  onArchived,
  draftText,
  onDraftChange,
}: ChatViewProps) {
  const client = useQueryClient()
  const csrf = useCsrfToken()

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
        <button
          type="button"
          className="quiet danger"
          onClick={() => archiveChat.mutate()}
          disabled={archiveChat.isPending || createMessage.isPending || !csrf}
          aria-label="Архивировать диалог"
        >
          {archiveChat.isPending ? 'Архивируем…' : 'Архивировать'}
        </button>
      </header>
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
            Сообщения сохраняются отдельно от запусков. Запуск pipeline появится
            в следующих этапах.
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
import { useEffect, useRef } from 'react'
