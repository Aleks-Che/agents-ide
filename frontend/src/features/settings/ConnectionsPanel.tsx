import { EditorError } from './EditorError'
import { Modal } from '../../app/Modal'
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError } from '../../api/client'
import { connectionsApi, describeError } from '../../api/settings'
import type { ProviderConnection } from '../../api/settings'
import { formatDateTime } from '../../app/format'
import { useCsrfToken } from '../../app/session'

export function ConnectionsPanel() {
  const client = useQueryClient()
  const [createOpen, setCreateOpen] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const connections = useQuery({
    queryKey: ['connections', { includeArchived: false }],
    queryFn: () => connectionsApi.list({ includeArchived: false }),
  })

  return (
    <section className="panel" aria-labelledby="connections-heading">
      <header className="panel-header">
        <span className="section-label" id="connections-heading">
          LLM-подключения
        </span>
        <button
          type="button"
          className="quiet"
          onClick={() => setCreateOpen(true)}
        >
          + Новое
        </button>
      </header>
      <ConnectionList
        connections={connections.data ?? []}
        loading={connections.isLoading}
        error={connections.error ? describeError(connections.error) : null}
        onRetry={() => void connections.refetch()}
        onEdit={(connection) => setEditingId(connection.id)}
      />
      {createOpen ? (
        <CreateConnectionDialog
          onClose={() => setCreateOpen(false)}
          onCreated={() => {
            setCreateOpen(false)
            void client.invalidateQueries({ queryKey: ['connections'] })
          }}
        />
      ) : null}
      {editingId ? (
        <EditConnectionDialog
          connectionId={editingId}
          onClose={() => setEditingId(null)}
          onSaved={() => {
            setEditingId(null)
            void client.invalidateQueries({ queryKey: ['connections'] })
          }}
        />
      ) : null}
    </section>
  )
}

interface ConnectionListProps {
  connections: ProviderConnection[]
  loading: boolean
  error: string | null
  onRetry: () => void
  onEdit: (connection: ProviderConnection) => void
}

function ConnectionList({
  connections,
  loading,
  error,
  onRetry,
  onEdit,
}: ConnectionListProps) {
  if (loading) {
    return (
      <p className="panel-empty" role="status">
        Загружаем подключения…
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
  if (connections.length === 0) {
    return (
      <p className="panel-empty">
        Подключений нет. Добавьте OpenAI-совместимый URL, чтобы вызывать LLM
        напрямую.
      </p>
    )
  }
  return (
    <ul className="panel-list" aria-label="LLM-подключения">
      {connections.map((connection) => (
        <li key={connection.id} className="profile-item">
          <header>
            <strong>{connection.name}</strong>
            <span className="pill">
              {connection.protocol.toUpperCase()} ·{' '}
              {labelKind(connection.provider_kind)}
            </span>
          </header>
          <code title={connection.base_url}>{connection.base_url}</code>
          <span className="meta">
            <span>{connection.has_secret ? 'Ключ сохранён' : 'Без ключа'}</span>
            <span>{formatDateTime(connection.updated_at)}</span>
          </span>
          <span className="meta">
            <span>
              Каталог · {connection.catalog_models.length} моделей ·{' '}
              {connection.manual_models.length} вручную
            </span>
            <button
              type="button"
              className="quiet"
              onClick={() => onEdit(connection)}
            >
              Параметры…
            </button>
          </span>
        </li>
      ))}
    </ul>
  )
}

function labelKind(kind: ProviderConnection['provider_kind']): string {
  if (kind === 'openai_compatible') return 'OpenAI-совместимое'
  if (kind === 'loopback') return 'Локальное'
  return kind
}

interface CreateConnectionDialogProps {
  onClose: () => void
  onCreated: () => void
}

function CreateConnectionDialog({
  onClose,
  onCreated,
}: CreateConnectionDialogProps) {
  const csrf = useCsrfToken()
  const [name, setName] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [secret, setSecret] = useState('')
  const [manualModels, setManualModels] = useState('')
  const create = useMutation({
    mutationFn: () =>
      connectionsApi.create(
        {
          name: name.trim(),
          base_url: baseUrl.trim(),
          secret: secret.trim() || null,
          manual_models: parseManualModels(manualModels),
        },
        csrf,
      ),
    onSuccess: onCreated,
  })
  return (
    <Modal
      onClose={onClose}
      busy={create.isPending}
      labelledBy="connection-create-title"
    >
      <form
        className="dialog"
        onSubmit={(event) => {
          event.preventDefault()
          create.mutate()
        }}
      >
        <header>
          <h3 id="connection-create-title">Новое LLM-подключение</h3>
          <button type="button" className="quiet" onClick={onClose}>
            ×
          </button>
        </header>
        <label htmlFor="connection-name">Название</label>
        <input
          id="connection-name"
          required
          maxLength={128}
          autoFocus
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
        <label htmlFor="connection-url">Base URL</label>
        <input
          id="connection-url"
          required
          spellCheck={false}
          placeholder="http://127.0.0.1:8080/v1 или https://api.openai.com/v1"
          value={baseUrl}
          onChange={(event) => setBaseUrl(event.target.value)}
        />
        <label htmlFor="connection-secret">Ключ</label>
        <input
          id="connection-secret"
          type="password"
          autoComplete="off"
          spellCheck={false}
          placeholder="оставьте пустым, если ключ не требуется"
          value={secret}
          onChange={(event) => setSecret(event.target.value)}
        />
        <label htmlFor="connection-manual">Ручные модели</label>
        <textarea
          id="connection-manual"
          rows={3}
          placeholder="Одна модель на строку"
          value={manualModels}
          onChange={(event) => setManualModels(event.target.value)}
        />
        <p className="hint">
          HTTP допустим только для loopback-адресов. Удалённые подключения
          требуют HTTPS.
        </p>
        {create.error ? (
          <p className="error" role="alert">
            {create.error instanceof ApiError
              ? create.error.body.message
              : describeError(create.error)}
          </p>
        ) : null}
        <footer>
          <button type="button" className="quiet" onClick={onClose}>
            Отмена
          </button>
          <button
            type="submit"
            disabled={
              create.isPending || !name.trim() || !baseUrl.trim() || !csrf
            }
          >
            {create.isPending ? 'Создаём…' : 'Создать'}
          </button>
        </footer>
      </form>
    </Modal>
  )
}

interface EditConnectionDialogProps {
  connectionId: string
  onClose: () => void
  onSaved: () => void
}

function EditConnectionDialog({
  connectionId,
  onClose,
  onSaved,
}: EditConnectionDialogProps) {
  const [reloadIndex, setReloadIndex] = useState(0)
  const connection = useQuery({
    queryKey: ['connection', connectionId],
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    queryFn: () => connectionsApi.get(connectionId),
  })

  if (!connection.data || !connection.isFetchedAfterMount) {
    return (
      <Modal onClose={onClose} busy={false} label="Настройки">
        <div className="dialog">
          <header>
            <h3>LLM-подключение</h3>
            <button type="button" className="quiet" onClick={onClose}>
              ×
            </button>
          </header>
          {connection.isFetching ? (
            <p role="status">Загружаем подключение…</p>
          ) : connection.error ? (
            <p className="error">{describeError(connection.error)}</p>
          ) : (
            <p>Подключение недоступно.</p>
          )}
        </div>
      </Modal>
    )
  }

  return (
    <EditConnectionForm
      key={`${connection.data.id}:${reloadIndex}`}
      connection={connection.data}
      onReload={async () => {
        const result = await connection.refetch()
        if (result.isSuccess) setReloadIndex((index) => index + 1)
      }}
      onClose={onClose}
      onSaved={onSaved}
    />
  )
}

interface EditConnectionFormProps {
  onReload: () => void
  connection: ProviderConnection
  onClose: () => void
  onSaved: () => void
}

function EditConnectionForm({
  connection: initial,
  onClose,
  onSaved,
  onReload,
}: EditConnectionFormProps) {
  const client = useQueryClient()
  const csrf = useCsrfToken()
  const [connection, setConnection] = useState(initial)
  const [name, setName] = useState(initial.name)
  const [baseUrl, setBaseUrl] = useState(initial.base_url)
  const [secret, setSecret] = useState('')
  const [manualModels, setManualModels] = useState(
    initial.manual_models.join('\n'),
  )
  const dirty =
    name.trim() !== connection.name ||
    baseUrl.trim() !== connection.base_url ||
    Boolean(secret) ||
    JSON.stringify(parseManualModels(manualModels)) !==
      JSON.stringify(connection.manual_models)
  function accept(updated: ProviderConnection) {
    setConnection(updated)
    client.setQueryData(['connection', updated.id], updated)
    void client.invalidateQueries({ queryKey: ['connections'] })
  }
  const save = useMutation({
    mutationFn: () =>
      connectionsApi.update(
        connection.id,
        {
          expected_version: connection.version,
          name: name.trim(),
          ...(baseUrl.trim() !== connection.base_url
            ? { base_url: baseUrl.trim() }
            : {}),
          ...(secret ? { secret } : {}),
          manual_models: parseManualModels(manualModels),
        },
        csrf,
      ),
    onSuccess: (updated) => {
      setSecret('')
      accept(updated)
      onSaved()
    },
  })
  const archive = useMutation({
    mutationFn: () =>
      connectionsApi.archive(connection.id, connection.version, csrf),
    onSuccess: (updated) => {
      accept(updated)
      onSaved()
    },
  })
  const test = useMutation({
    mutationFn: async () => {
      const result = await connectionsApi.test(connection.id, csrf)
      const updated = await connectionsApi.get(connection.id)
      return { result, updated }
    },
    onSuccess: ({ updated }) => {
      accept(updated)
      setName(updated.name)
      setBaseUrl(updated.base_url)
      setManualModels(updated.manual_models.join('\n'))
    },
  })
  const busy = save.isPending || archive.isPending || test.isPending
  return (
    <Modal onClose={onClose} busy={busy} labelledBy="connection-edit-title">
      <form
        className="dialog"
        onSubmit={(event) => {
          event.preventDefault()
          if (!busy && name.trim() && baseUrl.trim()) save.mutate()
        }}
      >
        <header>
          <h3 id="connection-edit-title">{connection.name}</h3>
          <button
            type="button"
            className="quiet"
            aria-label="Закрыть"
            onClick={onClose}
          >
            ×
          </button>
        </header>
        <label htmlFor="connection-edit-name">Название</label>
        <input
          id="connection-edit-name"
          required
          maxLength={128}
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
        <label htmlFor="connection-edit-url">Base URL</label>
        <input
          id="connection-edit-url"
          required
          spellCheck={false}
          value={baseUrl}
          onChange={(event) => setBaseUrl(event.target.value)}
        />
        <label htmlFor="connection-edit-secret">
          Новый ключ (оставьте пустым, чтобы не менять)
        </label>
        <input
          id="connection-edit-secret"
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={secret}
          onChange={(event) => setSecret(event.target.value)}
        />
        <span className="hint">
          {connection.has_secret ? 'Ключ сохранён · ••••••••' : 'Без ключа'}
        </span>
        <label htmlFor="connection-edit-manual">Ручные модели</label>
        <textarea
          id="connection-edit-manual"
          rows={3}
          value={manualModels}
          onChange={(event) => setManualModels(event.target.value)}
          placeholder="Одна модель на строку"
        />
        <div className="catalog-summary">
          <span>Модели из каталога</span>
          <ul aria-label="Модели подключения">
            {connection.catalog_models.map((model) => (
              <li key={model}>
                <code>{model}</code>
              </li>
            ))}
          </ul>
          {!connection.catalog_models.length ? (
            <span className="hint">
              Каталог пуст или недоступен. Можно использовать модель, указанную
              вручную.
            </span>
          ) : null}
        </div>
        <p className="hint">
          Ключ не возвращается через API. Тест делает короткий запрос к
          сохранённому подключению и может расходовать квоту провайдера. Сначала
          сохраните правки. Для теста используется первая ручная модель, а если
          список пуст — первая модель из каталога провайдера.
        </p>
        {test.data ? (
          <p
            role="status"
            className={test.data.result.status === 'ok' ? 'hint' : 'error'}
          >
            Тест · {test.data.result.status}
            {test.data.result.tested_model ? (
              <>
                {' '}
                · Модель: <code>{test.data.result.tested_model}</code>
              </>
            ) : null}
            {' · '}
            {test.data.result.detail}
          </p>
        ) : connection.last_test_status ? (
          <p className="hint">
            Последний тест · {connection.last_test_status} ·{' '}
            {formatDateTime(connection.last_test_at)}
          </p>
        ) : null}
        {[save.error, archive.error, test.error].map((error, index) => (
          <EditorError key={index} error={error} onReload={onReload} />
        ))}
        <footer>
          <button
            type="button"
            className="quiet danger"
            onClick={() => archive.mutate()}
            disabled={!csrf}
          >
            Архивировать
          </button>
          <button
            type="button"
            className="quiet"
            onClick={() => test.mutate()}
            disabled={!csrf || dirty}
          >
            {test.isPending ? 'Проверяем…' : 'Тест'}
          </button>
          <button
            type="submit"
            disabled={!csrf || !name.trim() || !baseUrl.trim()}
          >
            {save.isPending ? 'Сохраняем…' : 'Сохранить'}
          </button>
        </footer>
      </form>
    </Modal>
  )
}

function parseManualModels(text: string): string[] {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0)
}
