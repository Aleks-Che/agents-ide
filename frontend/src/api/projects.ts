import { ApiError, request } from './client'
import type { ApiOperations, ApiSchemas } from './generated'

export type { ApiSchemas, ApiOperations }
export type WorkspaceProbeResult = Record<string, unknown>

export type Project = ApiSchemas['Project']
export type Chat = ApiSchemas['Chat']
export type Message = ApiSchemas['Message']

export type ProjectsList = Project[]
export type ProjectChatsList = Chat[]
export type ChatMessagesList = Message[]

export interface ProjectsQuery {
  includeArchived?: boolean
}

export interface ChatsQuery {
  projectId: string
  includeArchived?: boolean
}

export interface MessagesQuery {
  chatId: string
  limit?: number
  latest?: boolean
  beforeId?: string
}

export const projectsApi = {
  list(query: ProjectsQuery = {}): Promise<Project[]> {
    const params = new URLSearchParams()
    if (query.includeArchived) params.set('include_archived', 'true')
    const search = params.toString()
    return request<Project[]>(`/projects${search ? `?${search}` : ''}`)
  },
  get(projectId: string): Promise<Project> {
    return request<Project>(`/projects/${projectId}`)
  },
  create(body: ApiSchemas['ProjectCreate'], csrf: string): Promise<Project> {
    return request<Project>(
      '/projects',
      { method: 'POST', body: JSON.stringify(body) },
      csrf,
    )
  },
  update(
    projectId: string,
    body: ApiSchemas['ProjectUpdate'],
    csrf: string,
  ): Promise<Project> {
    return request<Project>(
      `/projects/${projectId}`,
      { method: 'PATCH', body: JSON.stringify(body) },
      csrf,
    )
  },
  archive(
    projectId: string,
    body: ApiSchemas['ProjectArchive'],
    csrf: string,
  ): Promise<Project> {
    return request<Project>(
      `/projects/${projectId}/archive`,
      { method: 'POST', body: JSON.stringify(body) },
      csrf,
    )
  },
}

export const chatsApi = {
  list(query: ChatsQuery): Promise<Chat[]> {
    const params = new URLSearchParams()
    if (query.includeArchived) params.set('include_archived', 'true')
    const search = params.toString()
    return request<Chat[]>(
      `/projects/${query.projectId}/chats${search ? `?${search}` : ''}`,
    )
  },
  get(chatId: string): Promise<Chat> {
    return request<Chat>(`/chats/${chatId}`)
  },
  create(
    projectId: string,
    body: ApiSchemas['ChatCreate'],
    csrf: string,
  ): Promise<Chat> {
    return request<Chat>(
      `/projects/${projectId}/chats`,
      { method: 'POST', body: JSON.stringify(body) },
      csrf,
    )
  },
  update(
    chatId: string,
    body: ApiSchemas['ChatUpdate'],
    csrf: string,
  ): Promise<Chat> {
    return request<Chat>(
      `/chats/${chatId}`,
      { method: 'PATCH', body: JSON.stringify(body) },
      csrf,
    )
  },
  archive(
    chatId: string,
    body: ApiSchemas['ChatArchive'],
    csrf: string,
  ): Promise<Chat> {
    return request<Chat>(
      `/chats/${chatId}/archive`,
      { method: 'POST', body: JSON.stringify(body) },
      csrf,
    )
  },
}

export const messagesApi = {
  list(query: MessagesQuery): Promise<Message[]> {
    const params = new URLSearchParams()
    if (query.limit) params.set('limit', String(query.limit))
    if (query.latest) params.set('latest', 'true')
    if (query.beforeId) params.set('before_id', query.beforeId)
    const search = params.toString()
    return request<Message[]>(
      `/chats/${query.chatId}/messages${search ? `?${search}` : ''}`,
    )
  },
  get(messageId: string): Promise<Message> {
    return request<Message>(`/messages/${messageId}`)
  },
  create(
    chatId: string,
    body: ApiSchemas['MessageCreate'],
    csrf: string,
  ): Promise<Message> {
    return request<Message>(
      `/chats/${chatId}/messages`,
      { method: 'POST', body: JSON.stringify(body) },
      csrf,
    )
  },
  update(
    messageId: string,
    body: ApiSchemas['MessageUpdate'],
    csrf: string,
  ): Promise<Message> {
    return request<Message>(
      `/messages/${messageId}`,
      { method: 'PATCH', body: JSON.stringify(body) },
      csrf,
    )
  },
  archive(
    messageId: string,
    body: ApiSchemas['ChatArchive'],
    csrf: string,
  ): Promise<Message> {
    return request<Message>(
      `/messages/${messageId}/archive`,
      { method: 'POST', body: JSON.stringify(body) },
      csrf,
    )
  },
}

export function describeError(error: unknown): string {
  if (error instanceof ApiError) return error.body.message
  if (error instanceof Error) return error.message
  return 'Неизвестная ошибка'
}
