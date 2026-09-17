import { useState } from 'react'
import {
  HarnessExecutionSettings,
  type HarnessExecutionOptions,
} from './HarnessExecutionSettings'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Modal } from '../../app/Modal'
import { useCsrfToken } from '../../app/session'
import {
  describeError,
  harnessApi,
  type HarnessProfile,
} from '../../api/settings'
import { EditorError } from './EditorError'
import { defaultHarnessModel, useInstalledHarnesses } from './harness_queries'

export function HarnessProfilesPanel() {
  const [editingId, setEditingId] = useState<string | null>(null)
  const harnesses = useInstalledHarnesses()
  return (
    <section className="panel" aria-labelledby="harness-heading">
      <header className="panel-header">
        <span className="section-label" id="harness-heading">
          Установленные harness
        </span>
        <button
          type="button"
          className="quiet"
          disabled={harnesses.isFetching}
          onClick={() => void harnesses.refetch()}
        >
          Обновить список
        </button>
      </header>
      <p className="hint">
        Codex и OpenCode находятся автоматически. Выберите harness, чтобы
        открыть настройки и модель по умолчанию.
      </p>
      {harnesses.isLoading ? (
        <p className="panel-empty" role="status">
          Ищем установленные harness…
        </p>
      ) : null}
      {harnesses.error ? (
        <p className="error" role="alert">
          {describeError(harnesses.error)}
        </p>
      ) : null}
      {harnesses.isSuccess && !harnesses.data.length ? (
        <p className="panel-empty">
          Harness не найдены. Установите Codex или OpenCode под текущим
          пользователем и обновите список.
        </p>
      ) : null}
      <ul className="panel-list" aria-label="Установленные harness">
        {(harnesses.data ?? []).map((harness) => (
          <li key={harness.id} className="profile-item harness-item">
            <button
              type="button"
              className="quiet harness-installation"
              aria-label={
                harness.harness_kind === 'codex' ? 'Codex' : 'OpenCode'
              }
              onClick={() => setEditingId(harness.id)}
            >
              <strong>
                {harness.harness_kind === 'codex' ? 'Codex' : 'OpenCode'}
              </strong>
              <code>{harness.executable_path}</code>
              <span className="muted">
                Модель по умолчанию ·{' '}
                {defaultHarnessModel(harness) || 'не выбрана'}
              </span>
            </button>
          </li>
        ))}
      </ul>
      {editingId ? (
        <HarnessSettingsDialog
          harnessId={editingId}
          onClose={() => setEditingId(null)}
        />
      ) : null}
    </section>
  )
}

function HarnessSettingsDialog({
  harnessId,
  onClose,
}: {
  harnessId: string
  onClose: () => void
}) {
  const csrf = useCsrfToken()
  const [reloadIndex, setReloadIndex] = useState(0)
  const harness = useQuery({
    queryKey: ['harness_settings', harnessId],
    queryFn: async () => {
      let catalogError: string | null = null
      try {
        await harnessApi.refreshCatalog(harnessId, csrf)
      } catch (error) {
        catalogError = describeError(error)
      }
      return { profile: await harnessApi.get(harnessId), catalogError }
    },
    enabled: Boolean(csrf),
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })
  if (!harness.data || !harness.isFetchedAfterMount) {
    return (
      <Modal onClose={onClose} busy={false} label="Настройки harness">
        <div className="dialog">
          <p role="status">
            {harness.error
              ? describeError(harness.error)
              : 'Загружаем настройки и модели harness…'}
          </p>
          <button type="button" onClick={onClose}>
            Закрыть
          </button>
        </div>
      </Modal>
    )
  }
  return (
    <HarnessSettingsForm
      key={`${harnessId}:${reloadIndex}`}
      initial={harness.data.profile}
      catalogError={harness.data.catalogError}
      onClose={onClose}
      onReload={async () => {
        const result = await harness.refetch()
        if (result.isSuccess) setReloadIndex((index) => index + 1)
      }}
    />
  )
}

function HarnessSettingsForm({
  initial,
  catalogError,
  onClose,
  onReload,
}: {
  initial: HarnessProfile
  catalogError: string | null
  onClose: () => void
  onReload: () => void
}) {
  const csrf = useCsrfToken()
  const client = useQueryClient()
  const [profile, setProfile] = useState(initial)
  const [model, setModel] = useState(defaultHarnessModel(initial))
  const [execution, setExecution] = useState<HarnessExecutionOptions>(
    initial.settings,
  )
  const [error, setError] = useState(catalogError)
  const save = useMutation({
    mutationFn: () =>
      harnessApi.update(
        profile.id,
        {
          expected_version: profile.version,
          settings: {
            ...profile.settings,
            ...execution,
            default_model: model || null,
          },
        },
        csrf,
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['installed_harnesses'] })
      void client.invalidateQueries({ queryKey: ['harness_profiles'] })
      onClose()
    },
  })
  const refresh = useMutation({
    mutationFn: async () => {
      try {
        await harnessApi.refreshCatalog(profile.id, csrf, true)
        setError(null)
      } catch (cause) {
        setError(describeError(cause))
      }
      return harnessApi.get(profile.id)
    },
    onSuccess: (updated) => {
      setProfile(updated)
      void client.invalidateQueries({
        queryKey: ['harness_catalog', profile.id],
      })
    },
  })
  const busy = save.isPending || refresh.isPending
  const valid =
    !model ||
    model === defaultHarnessModel(profile) ||
    (!error && profile.catalog_models.includes(model))
  return (
    <Modal onClose={onClose} busy={busy} labelledBy="harness-edit-title">
      <form
        className="dialog"
        onSubmit={(event) => {
          event.preventDefault()
          if (!busy && valid) save.mutate()
        }}
      >
        <header>
          <h3 id="harness-edit-title">
            {profile.harness_kind === 'codex' ? 'Codex' : 'OpenCode'} ·
            настройки
          </h3>
          <button
            type="button"
            className="quiet"
            aria-label="Закрыть"
            onClick={onClose}
          >
            ×
          </button>
        </header>
        <label htmlFor="harness-path">Исполняемый файл</label>
        <input
          id="harness-path"
          readOnly
          value={profile.executable_path ?? ''}
        />
        <label htmlFor="harness-default-model">Модель по умолчанию</label>
        <select
          id="harness-default-model"
          value={model}
          disabled={busy}
          onChange={(event) => setModel(event.target.value)}
        >
          <option value="">Выбирать при добавлении в группу</option>
          {model && !profile.catalog_models.includes(model) ? (
            <option value={model} disabled>
              Недоступна · {model}
            </option>
          ) : null}
          {profile.catalog_models.map((id) => (
            <option key={id} value={id}>
              {id}
            </option>
          ))}
        </select>
        <p className="hint">
          Модели загружаются из самой harness с её подключениями и авторизацией.
          Выбранная модель подставляется при добавлении harness в группу; у
          каждого участника её можно изменить.
        </p>
        <HarnessExecutionSettings
          kind={profile.harness_kind}
          value={execution}
          onChange={setExecution}
          disabled={busy}
        />
        {error ? (
          <p className="error" role="alert">
            {error}
          </p>
        ) : null}
        {!error && profile.catalog_models.length === 0 ? (
          <p className="hint">
            Нет доступных моделей. Настройте провайдера и вход в самой harness.
          </p>
        ) : null}
        <EditorError error={save.error ?? refresh.error} onReload={onReload} />
        <footer>
          <button
            type="button"
            className="quiet"
            disabled={busy || !csrf}
            onClick={() => refresh.mutate()}
          >
            {refresh.isPending ? 'Загружаем…' : 'Обновить модели'}
          </button>
          <button type="submit" disabled={busy || !csrf || !valid}>
            {save.isPending ? 'Сохраняем…' : 'Сохранить'}
          </button>
        </footer>
      </form>
    </Modal>
  )
}
