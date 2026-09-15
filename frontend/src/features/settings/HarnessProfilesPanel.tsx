import { EditorError } from './EditorError'
import { Modal } from '../../app/Modal'
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError } from '../../api/client'
import { describeError, harnessApi } from '../../api/settings'
import type { HarnessProfile } from '../../api/settings'
import { formatDateTime } from '../../app/format'
import { useCsrfToken } from '../../app/session'

export function HarnessProfilesPanel() {
  const client = useQueryClient()
  const [createOpen, setCreateOpen] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const profiles = useQuery({
    queryKey: ['harness_profiles', { includeArchived: false }],
    queryFn: () => harnessApi.list({ includeArchived: false }),
  })

  return (
    <section className="panel" aria-labelledby="harness-heading">
      <header className="panel-header">
        <span className="section-label" id="harness-heading">
          Профили harness
        </span>
        <button
          type="button"
          className="quiet"
          onClick={() => setCreateOpen(true)}
        >
          + Новый
        </button>
      </header>
      <ProfileList
        profiles={profiles.data ?? []}
        loading={profiles.isLoading}
        error={profiles.error ? describeError(profiles.error) : null}
        onRetry={() => void profiles.refetch()}
        onEdit={(profile) => setEditingId(profile.id)}
      />
      {createOpen ? (
        <CreateProfileDialog
          onClose={() => setCreateOpen(false)}
          onCreated={() => {
            setCreateOpen(false)
            void client.invalidateQueries({ queryKey: ['harness_profiles'] })
          }}
        />
      ) : null}
      {editingId ? (
        <EditProfileDialog
          profileId={editingId}
          onClose={() => setEditingId(null)}
          onSaved={() => {
            setEditingId(null)
            void client.invalidateQueries({ queryKey: ['harness_profiles'] })
          }}
        />
      ) : null}
    </section>
  )
}

interface ProfileListProps {
  profiles: HarnessProfile[]
  loading: boolean
  error: string | null
  onRetry: () => void
  onEdit: (profile: HarnessProfile) => void
}

function ProfileList({
  profiles,
  loading,
  error,
  onRetry,
  onEdit,
}: ProfileListProps) {
  if (loading) {
    return (
      <p className="panel-empty" role="status">
        Загружаем профили…
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
  if (profiles.length === 0) {
    return (
      <p className="panel-empty">
        Harness-профилей нет. Добавьте профиль OpenCode. Поддержка Codex ещё не
        реализована.
      </p>
    )
  }
  return (
    <ul className="panel-list" aria-label="Harness-профили">
      {profiles.map((profile) => (
        <li key={profile.id} className="profile-item">
          <header>
            <strong>{profile.name}</strong>
            <span className="pill">{labelKind(profile.harness_kind)}</span>
          </header>
          <span className="muted">ID · {profile.id}</span>
          <span className="meta">
            <span>{formatDateTime(profile.created_at)}</span>
            {profile.executable_path ? (
              <code title={profile.executable_path}>
                {profile.executable_path}
              </code>
            ) : (
              <span className="muted">
                путь не указан — тест и запуск недоступны
              </span>
            )}
          </span>
          <span className="meta">
            <span>
              Каталог · {profile.catalog_models.length} моделей
              {profile.catalog_fetched_at
                ? ` · обновлён ${formatDateTime(profile.catalog_fetched_at)}`
                : ''}
            </span>
            <button
              type="button"
              className="quiet"
              onClick={() => onEdit(profile)}
            >
              Параметры…
            </button>
          </span>
        </li>
      ))}
    </ul>
  )
}

function labelKind(kind: HarnessProfile['harness_kind']): string {
  if (kind === 'opencode') return 'OpenCode'
  if (kind === 'codex') return 'Codex'
  return kind
}

interface CreateProfileDialogProps {
  onClose: () => void
  onCreated: () => void
}

function CreateProfileDialog({ onClose, onCreated }: CreateProfileDialogProps) {
  const csrf = useCsrfToken()
  const [name, setName] = useState('')
  const [kind, setKind] = useState<'opencode' | 'codex'>('opencode')
  const [executablePath, setExecutablePath] = useState('')
  const [noTools, setNoTools] = useState(false)
  const create = useMutation({
    mutationFn: () =>
      harnessApi.create(
        {
          name: name.trim(),
          harness_kind: kind,
          executable_path: executablePath.trim() || null,
          settings:
            kind === 'opencode' && noTools
              ? { permission_mode: 'no_tools' }
              : {},
        },
        csrf,
      ),
    onSuccess: onCreated,
  })
  return (
    <Modal
      onClose={onClose}
      busy={create.isPending}
      labelledBy="harness-create-title"
    >
      <form
        className="dialog"
        onSubmit={(event) => {
          event.preventDefault()
          create.mutate()
        }}
      >
        <header>
          <h3 id="harness-create-title">Новый harness-профиль</h3>
          <button type="button" className="quiet" onClick={onClose}>
            ×
          </button>
        </header>
        <label htmlFor="harness-name">Название</label>
        <input
          id="harness-name"
          required
          maxLength={128}
          autoFocus
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
        <label htmlFor="harness-kind">Тип</label>
        <select
          id="harness-kind"
          value={kind}
          onChange={(event) =>
            setKind(event.target.value === 'codex' ? 'codex' : 'opencode')
          }
        >
          <option value="opencode">OpenCode</option>
          <option value="codex">Codex (запуск пока недоступен)</option>
        </select>
        <label htmlFor="harness-path">Путь к исполняемому файлу</label>
        <input
          id="harness-path"
          spellCheck={false}
          placeholder="C:\tools\opencode.exe"
          value={executablePath}
          onChange={(event) => setExecutablePath(event.target.value)}
        />
        <p className="hint">
          Для теста и запуска нужен полный путь к нативному исполняемому файлу.
          Скрипты .cmd, .bat и .ps1 не поддерживаются.
        </p>
        {kind === 'opencode' ? (
          <label className="enabled-toggle">
            <input
              type="checkbox"
              checked={noTools}
              onChange={(event) => setNoTools(event.target.checked)}
            />
            Только ответы модели, без инструментов (no_tools)
          </label>
        ) : null}
        <p className="hint">
          OpenCode пока можно запускать только в режиме no_tools. Запись в
          проект и доступ к инструментам ещё не прошли приёмку.
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
            disabled={create.isPending || !name.trim() || !csrf}
          >
            {create.isPending ? 'Создаём…' : 'Создать'}
          </button>
        </footer>
      </form>
    </Modal>
  )
}

interface EditProfileDialogProps {
  profileId: string
  onClose: () => void
  onSaved: () => void
}

function EditProfileDialog({
  profileId,
  onClose,
  onSaved,
}: EditProfileDialogProps) {
  const [reloadIndex, setReloadIndex] = useState(0)
  const profile = useQuery({
    queryKey: ['harness_profile', profileId],
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    queryFn: () => harnessApi.get(profileId),
  })

  if (!profile.data || !profile.isFetchedAfterMount) {
    return (
      <Modal onClose={onClose} busy={false} label="Настройки">
        <div className="dialog">
          <header>
            <h3>Профиль harness</h3>
            <button type="button" className="quiet" onClick={onClose}>
              ×
            </button>
          </header>
          {profile.isFetching ? (
            <p role="status">Загружаем профиль…</p>
          ) : profile.error ? (
            <p className="error">{describeError(profile.error)}</p>
          ) : (
            <p>Профиль недоступен.</p>
          )}
        </div>
      </Modal>
    )
  }

  return (
    <EditProfileForm
      key={`${profile.data.id}:${reloadIndex}`}
      profile={profile.data}
      onReload={async () => {
        const result = await profile.refetch()
        if (result.isSuccess) setReloadIndex((index) => index + 1)
      }}
      onClose={onClose}
      onSaved={onSaved}
    />
  )
}

interface EditProfileFormProps {
  onReload: () => void
  profile: HarnessProfile
  onClose: () => void
  onSaved: () => void
}

function EditProfileForm({
  profile: initial,
  onClose,
  onSaved,
  onReload,
}: EditProfileFormProps) {
  const client = useQueryClient()
  const csrf = useCsrfToken()
  const [profile, setProfile] = useState(initial)
  const [name, setName] = useState(initial.name)
  const [executablePath, setExecutablePath] = useState(
    initial.executable_path ?? '',
  )
  const [catalogTtl, setCatalogTtl] = useState(
    String(initial.catalog_ttl_seconds),
  )
  const [noTools, setNoTools] = useState(
    initial.settings.permission_mode === 'no_tools',
  )
  const catalog = useQuery({
    queryKey: ['harness_catalog', profile.id],
    queryFn: () => harnessApi.catalog(profile.id),
  })
  const dirty =
    name.trim() !== profile.name ||
    executablePath.trim() !== (profile.executable_path ?? '') ||
    Number(catalogTtl) !== profile.catalog_ttl_seconds ||
    noTools !== (profile.settings.permission_mode === 'no_tools')
  function accept(updated: HarnessProfile) {
    setProfile(updated)
    client.setQueryData(['harness_profile', updated.id], updated)
    void client.invalidateQueries({ queryKey: ['harness_profiles'] })
    void client.invalidateQueries({ queryKey: ['harness_catalog', updated.id] })
  }
  const save = useMutation({
    mutationFn: () =>
      harnessApi.update(
        profile.id,
        {
          expected_version: profile.version,
          name: name.trim(),
          ...(executablePath.trim() !== (profile.executable_path ?? '')
            ? { executable_path: executablePath.trim() || null }
            : {}),
          ...(Number(catalogTtl) !== profile.catalog_ttl_seconds
            ? { catalog_ttl_seconds: Number(catalogTtl) }
            : {}),
          ...(noTools !== (profile.settings.permission_mode === 'no_tools')
            ? {
                settings: {
                  ...profile.settings,
                  permission_mode: noTools ? 'no_tools' : null,
                },
              }
            : {}),
        },
        csrf,
      ),
    onSuccess: (updated) => {
      accept(updated)
      onSaved()
    },
  })
  const archive = useMutation({
    mutationFn: () => harnessApi.archive(profile.id, profile.version, csrf),
    onSuccess: (updated) => {
      accept(updated)
      onSaved()
    },
  })
  const probe = useMutation({
    mutationFn: async () => {
      const result = await harnessApi.probe(profile.id, csrf)
      const updated = await harnessApi.get(profile.id)
      return { result, updated }
    },
    onSuccess: ({ updated }) => {
      accept(updated)
      setName(updated.name)
      setExecutablePath(updated.executable_path ?? '')
      setCatalogTtl(String(updated.catalog_ttl_seconds))
      setNoTools(updated.settings.permission_mode === 'no_tools')
    },
  })
  const busy = save.isPending || archive.isPending || probe.isPending
  const valid =
    name.trim() &&
    Number.isInteger(Number(catalogTtl)) &&
    Number(catalogTtl) >= 60 &&
    Number(catalogTtl) <= 86400
  return (
    <Modal onClose={onClose} busy={busy} labelledBy="harness-edit-title">
      <form
        className="dialog"
        onSubmit={(event) => {
          event.preventDefault()
          if (valid && !busy) save.mutate()
        }}
      >
        <header>
          <h3 id="harness-edit-title">{profile.name}</h3>
          <button
            type="button"
            className="quiet"
            aria-label="Закрыть"
            onClick={onClose}
          >
            ×
          </button>
        </header>
        <label htmlFor="harness-edit-name">Название</label>
        <input
          id="harness-edit-name"
          required
          maxLength={128}
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
        <label htmlFor="harness-edit-path">Путь</label>
        <input
          id="harness-edit-path"
          spellCheck={false}
          value={executablePath}
          onChange={(event) => setExecutablePath(event.target.value)}
        />
        <label htmlFor="harness-edit-ttl">TTL каталога, секунд</label>
        <input
          id="harness-edit-ttl"
          type="number"
          required
          min={60}
          max={86400}
          step={1}
          value={catalogTtl}
          onChange={(event) => setCatalogTtl(event.target.value)}
        />
        {profile.harness_kind === 'opencode' ? (
          <label className="enabled-toggle">
            <input
              type="checkbox"
              checked={noTools}
              onChange={(event) => setNoTools(event.target.checked)}
            />
            Только ответы модели, без инструментов (no_tools)
          </label>
        ) : (
          <p className="hint">Запуск Codex ещё не реализован.</p>
        )}
        <p className="hint">
          OpenCode пока выполняет только запросы без инструментов (no_tools).
          Каталог не подтверждает доступ к модели. Тест проверяет сохранённый
          профиль; сначала сохраните правки.
        </p>
        {catalog.data ? (
          <div className="catalog-summary">
            <span>
              Каталог ·{' '}
              {catalog.data.status === 'fresh'
                ? 'актуальный'
                : catalog.data.status === 'stale'
                  ? 'устарел'
                  : 'не проверен'}
            </span>
            <ul aria-label="Модели harness">
              {catalog.data.models.map((model) => (
                <li key={model.id}>
                  <code>{model.id}</code>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        {catalog.error ? (
          <p className="error" role="alert">
            {describeError(catalog.error)}
          </p>
        ) : null}
        {probe.data ? (
          <p
            role="status"
            className={probe.data.result.status === 'ok' ? 'hint' : 'error'}
          >
            Тест · {probe.data.result.status} {probe.data.result.version}{' '}
            {probe.data.result.detail}
          </p>
        ) : profile.last_test_status ? (
          <p className="hint">
            Последний тест · {profile.last_test_status} ·{' '}
            {formatDateTime(profile.last_test_at)}
          </p>
        ) : null}
        {[save.error, archive.error, probe.error].map((error, index) => (
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
            onClick={() => probe.mutate()}
            disabled={!csrf || dirty}
          >
            {probe.isPending ? 'Проверяем…' : 'Тест'}
          </button>
          <button type="submit" disabled={!csrf || !valid}>
            {save.isPending ? 'Сохраняем…' : 'Сохранить'}
          </button>
        </footer>
      </form>
    </Modal>
  )
}
