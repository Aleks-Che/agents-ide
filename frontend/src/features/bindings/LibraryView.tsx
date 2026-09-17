import { lazy, Suspense, useCallback, useMemo, useState } from 'react'
import {
  contextMenuHandlers,
  type ContextMenuTarget,
} from '../../app/context_menu'
import { ContextMenu } from '../../app/ContextMenu'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError } from '../../api/client'
import {
  bindingsApi,
  presetsApi,
  templatesApi,
  type PipelineBinding,
  type PipelineTemplate,
  type PipelineVersion,
  type PresetSummary,
} from '../../api/bindings'
import { groupsApi, harnessApi, connectionsApi } from '../../api/settings'
import type { Project } from '../../api/projects'
import { useCsrfToken } from '../../app/session'
import { formatDateTime, shortHash } from '../../app/format'
import { BindingEditor, type BindingEditorResources } from './BindingEditor'
import { Modal } from '../../app/Modal'
const GraphEditor = lazy(() =>
  import('../pipelines/GraphEditor').then((module) => ({
    default: module.GraphEditor,
  })),
)

interface LibraryViewProps {
  projects: Project[]
  selectedProjectId: string | null
}

export function LibraryView({ projects, selectedProjectId }: LibraryViewProps) {
  const client = useQueryClient()
  const csrf = useCsrfToken()
  const templates = useQuery({
    queryKey: ['templates', { includeArchived: false }],
    queryFn: () => templatesApi.list({ includeArchived: false }),
  })
  const bindings = useQuery({
    queryKey: [
      'bindings',
      { projectId: selectedProjectId ?? '', includeArchived: false },
    ],
    queryFn: () =>
      bindingsApi.list({
        projectId: selectedProjectId ?? undefined,
        includeArchived: false,
      }),
    enabled: Boolean(selectedProjectId),
  })
  const presets = useQuery({
    queryKey: ['presets'],
    queryFn: () => presetsApi.list(),
  })
  const groups = useQuery({
    queryKey: ['model_groups', { kind: 'agent', includeArchived: true }],
    queryFn: () => groupsApi.list({ kind: 'agent', includeArchived: true }),
  })
  const llmGroups = useQuery({
    queryKey: ['model_groups', { kind: 'llm', includeArchived: true }],
    queryFn: () => groupsApi.list({ kind: 'llm', includeArchived: true }),
  })
  const harnesses = useQuery({
    queryKey: ['harness_profiles', { includeArchived: true }],
    queryFn: () => harnessApi.list({ includeArchived: true }),
  })
  const connections = useQuery({
    queryKey: ['connections', { includeArchived: true }],
    queryFn: () => connectionsApi.list({ includeArchived: true }),
  })

  const resources = useMemo<BindingEditorResources>(() => {
    const groupList = (groups.data ?? []) as Array<{
      id: string
      name: string
      kind: 'agent' | 'llm'
      members: Array<{
        id: string
        member_index: number
        enabled: boolean
        harness_profile_id: string | null
        provider_connection_id: string | null
        model_id: string
        params: Record<string, unknown>
      }>
      archived: boolean
      revision: number
    }>
    const llmGroupList = (llmGroups.data ?? []) as typeof groupList
    return {
      groups: {
        agent: groupList.filter((group) => group.kind === 'agent'),
        llm: llmGroupList.filter((group) => group.kind === 'llm'),
      },
      harnesses: (harnesses.data ?? []).map((profile) => ({
        id: profile.id,
        name: profile.name,
        archived: profile.archived,
      })),
      connections: (connections.data ?? []).map((connection) => ({
        id: connection.id,
        name: connection.name,
        archived: connection.archived,
        manual_models: connection.manual_models,
        catalog_models: connection.catalog_models,
      })),
    }
  }, [groups.data, llmGroups.data, harnesses.data, connections.data])

  const selectedProject =
    projects.find((p) => p.id === selectedProjectId) ?? null
  const [openBindingId, setOpenBindingId] = useState<string | null>(null)
  const [copyPreset, setCopyPreset] = useState<PresetSummary | null>(null)
  const [copyTemplate, setCopyTemplate] = useState<PipelineTemplate | null>(
    null,
  )
  const [menu, setMenu] = useState<
    | (ContextMenuTarget & {
        template: PipelineTemplate
        preset?: PresetSummary
      })
    | null
  >(null)
  const closeMenu = useCallback(() => setMenu(null), [])
  const [editPreset, setEditPreset] = useState(false)
  const [openTemplate, setOpenTemplate] = useState<PipelineTemplate | null>(
    null,
  )
  const [editor, setEditor] = useState<{
    templateId: string
    version?: PipelineVersion
  } | null>(null)
  const [creating, setCreating] = useState(false)
  const [templateName, setTemplateName] = useState('')
  const createTemplate = useMutation({
    mutationFn: () => templatesApi.create({ name: templateName.trim() }, csrf),
    onSuccess: (created) => {
      setCreating(false)
      setTemplateName('')
      void client.invalidateQueries({ queryKey: ['templates'] })
      setEditor({ templateId: created.id })
    },
  })
  const copyPresetMutation = useMutation({
    mutationFn: ({ preset, name }: { preset: PresetSummary; name: string }) =>
      presetsApi.copy(preset.id, csrf, name),
    onSuccess: (created) => {
      setCopyPreset(null)
      void client.invalidateQueries({ queryKey: ['templates'] })
      if (editPreset) setEditor({ templateId: created.id })
      setEditPreset(false)
    },
  })
  const copyTemplateMutation = useMutation({
    mutationFn: ({
      template,
      name,
    }: {
      template: PipelineTemplate
      name: string
    }) =>
      templatesApi.copy(
        template.id,
        { name, expected_version: template.version },
        csrf,
      ),
    onSuccess: (created) => {
      setCopyTemplate(null)
      void client.invalidateQueries({ queryKey: ['templates'] })
      if (editPreset) setEditor({ templateId: created.id })
      setEditPreset(false)
    },
  })
  const removeTemplate = useMutation({
    mutationFn: (template: PipelineTemplate) =>
      templatesApi.archive(template.id, template.version, csrf),
    onSuccess: (removed) => {
      client.setQueryData<PipelineTemplate[]>(
        ['templates', { includeArchived: false }],
        (previous = []) => previous.filter((item) => item.id !== removed.id),
      )
      void client.invalidateQueries({ queryKey: ['templates'] })
    },
    onError: () => {
      void client.invalidateQueries({ queryKey: ['templates'] })
    },
  })
  const openMenu = (
    target: ContextMenuTarget,
    template: PipelineTemplate,
    preset?: PresetSummary,
  ) => {
    if (!removeTemplate.isPending) removeTemplate.reset()
    setMenu({ ...target, template, preset })
  }
  const beginCopy = (edit: boolean) => {
    if (!menu) return
    setEditPreset(edit)
    copyPresetMutation.reset()
    copyTemplateMutation.reset()
    if (menu.preset) setCopyPreset(menu.preset)
    else setCopyTemplate(menu.template)
  }

  const templatesData = (templates.data ?? []) as PipelineTemplate[]
  const presetsData = (presets.data ?? []) as PresetSummary[]
  const bindingsData = (bindings.data ?? []) as PipelineBinding[]

  return (
    <section className="main-pane" aria-labelledby="library-title">
      <header className="main-header">
        <div>
          <span className="eyebrow">ШАБЛОНЫ</span>
          <h2 id="library-title">Шаблоны, версии и привязки</h2>
          <span className="muted">
            Копируйте встроенный пресет в пользовательский шаблон,
            просматривайте версии и настраивайте выбор моделей для проекта.
          </span>
        </div>
        <button
          type="button"
          disabled={!csrf}
          onClick={() => setCreating(true)}
        >
          Новый шаблон
        </button>
      </header>
      <div className="library-grid">
        {[groups, llmGroups, harnesses, connections].some(
          (query) => query.isLoading,
        ) ? (
          <p role="status">Загружаем исполнителей…</p>
        ) : null}
        {[groups, llmGroups, harnesses, connections].map((query, index) =>
          query.error ? (
            <p key={index} role="alert" className="error">
              {renderLibraryError(query.error)}
            </p>
          ) : null,
        )}
        <section className="panel" aria-labelledby="templates-heading">
          <header className="panel-header">
            <span className="section-label" id="templates-heading">
              Шаблоны и пресеты
            </span>
          </header>
          {templates.isLoading || presets.isLoading ? (
            <p role="status" className="panel-empty">
              Загружаем шаблоны…
            </p>
          ) : templates.error || presets.error ? (
            <div className="panel-empty">
              <p className="error">
                {renderLibraryError(templates.error ?? presets.error)}
              </p>
              <button
                type="button"
                className="quiet"
                onClick={() => {
                  void templates.refetch()
                  void presets.refetch()
                }}
              >
                Повторить
              </button>
            </div>
          ) : (
            <ol className="panel-list">
              {presetsData.map((preset) => (
                <li
                  key={preset.id}
                  className="profile-item"
                  tabIndex={0}
                  {...contextMenuHandlers((target) => {
                    const template = templatesData.find(
                      (item) => item.id === preset.template_id,
                    )
                    if (template) openMenu(target, template, preset)
                  })}
                >
                  <header>
                    <strong>{preset.name}</strong>
                    <span className="pill">встроенный</span>
                  </header>
                  <span className="muted">
                    {preset.description || 'Без описания'}
                  </span>
                  <span className="meta">
                    <span>
                      Роли: {Object.keys(preset.roles).join(', ') || '—'} ·
                      версия {shortHash(preset.preset_version, 12)}
                    </span>
                  </span>
                </li>
              ))}
              {templatesData.map((tpl) => (
                <li
                  key={tpl.id}
                  className="profile-item"
                  tabIndex={0}
                  {...contextMenuHandlers((target) =>
                    openMenu(
                      target,
                      tpl,
                      presetsData.find(
                        (preset) => preset.template_id === tpl.id,
                      ),
                    ),
                  )}
                >
                  <header>
                    <strong>{tpl.name}</strong>
                    <span className="pill">
                      {tpl.kind === 'system' ? 'системный' : 'пользов.'}
                    </span>
                  </header>
                  {tpl.description ? (
                    <span className="muted">{tpl.description}</span>
                  ) : null}
                  <span className="meta">
                    <span>
                      Обновлён {formatDateTime(tpl.updated_at)} · ревизия{' '}
                      {tpl.version}
                    </span>
                  </span>
                </li>
              ))}
              {presetsData.length === 0 && templatesData.length === 0 ? (
                <li className="panel-empty">
                  Пресеты пока не установлены. Перезапустите API для
                  переустановки.
                </li>
              ) : null}
            </ol>
          )}
          {removeTemplate.isPending ? (
            <p role="status">Удаляем шаблон…</p>
          ) : null}
          {removeTemplate.error ? (
            <p role="alert" className="error">
              Не удалось удалить «{removeTemplate.variables?.name}».{' '}
              {renderLibraryError(removeTemplate.error)}
            </p>
          ) : null}
        </section>
        <section className="panel" aria-labelledby="bindings-heading">
          <header className="panel-header">
            <span className="section-label" id="bindings-heading">
              Привязки проекта
            </span>
          </header>
          {!selectedProject ? (
            <p className="panel-empty">
              Выберите проект слева, чтобы увидеть и редактировать его привязки.
            </p>
          ) : bindings.isLoading ? (
            <p role="status" className="panel-empty">
              Загружаем привязки…
            </p>
          ) : bindings.error ? (
            <div className="panel-empty">
              <p className="error">{renderLibraryError(bindings.error)}</p>
              <button
                type="button"
                className="quiet"
                onClick={() => void bindings.refetch()}
              >
                Повторить
              </button>
            </div>
          ) : bindingsData.length === 0 ? (
            <p className="panel-empty">
              Привязок ещё нет. Создайте первую из шаблона с опубликованной
              версией.
            </p>
          ) : (
            <ul className="panel-list" aria-label="Привязки проекта">
              {bindingsData.map((binding) => (
                <li key={binding.id} className="profile-item">
                  <header>
                    <strong>{binding.name}</strong>
                    <span className="pill">
                      версия {shortHash(binding.version_id, 7)}
                    </span>
                  </header>
                  <span className="meta">
                    <span>
                      Ревизия {binding.version} ·{' '}
                      {binding.archived ? 'архив' : 'активна'}
                    </span>
                    <span className="muted">
                      {binding.branch_policy === 'current'
                        ? 'ветка: current'
                        : 'ветка: run_branch'}{' '}
                      · {binding.dirty_policy}
                    </span>
                  </span>
                  <button
                    type="button"
                    className="quiet"
                    onClick={() => setOpenBindingId(binding.id)}
                    disabled={[groups, llmGroups, harnesses, connections].some(
                      (query) => !query.data || query.isError,
                    )}
                  >
                    Параметры…
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
      {menu ? (
        <ContextMenu
          target={menu}
          label={`Действия с шаблоном «${menu.template.name}»`}
          onClose={closeMenu}
          items={[
            {
              label: 'Копировать',
              onSelect: () => beginCopy(false),
              disabled: !csrf,
            },
            {
              label: 'Редактировать',
              onSelect: () => {
                if (menu.template.kind === 'system') beginCopy(true)
                else setEditor({ templateId: menu.template.id })
              },
              disabled: !csrf,
            },
            { label: 'Версии', onSelect: () => setOpenTemplate(menu.template) },
            {
              label: 'Удалить',
              onSelect: () => removeTemplate.mutate(menu.template),
              danger: true,
              disabled:
                !csrf ||
                removeTemplate.isPending ||
                menu.template.kind === 'system',
              title:
                menu.template.kind === 'system'
                  ? 'Встроенный шаблон нельзя удалить'
                  : 'Убрать шаблон из списка; существующие привязки сохранятся',
            },
          ]}
        />
      ) : null}
      {copyPreset ? (
        <TemplateCopyDialog
          editAfterCopy={editPreset}
          sourceName={copyPreset.name}
          isPreset
          csrf={csrf}
          pending={copyPresetMutation.isPending}
          error={copyPresetMutation.error}
          onClose={() => setCopyPreset(null)}
          onSubmit={(name) =>
            copyPresetMutation.mutate({ preset: copyPreset, name })
          }
        />
      ) : null}
      {copyTemplate ? (
        <TemplateCopyDialog
          editAfterCopy={editPreset}
          sourceName={copyTemplate.name}
          csrf={csrf}
          pending={copyTemplateMutation.isPending}
          error={copyTemplateMutation.error}
          onClose={() => setCopyTemplate(null)}
          onSubmit={(name) =>
            copyTemplateMutation.mutate({ template: copyTemplate, name })
          }
        />
      ) : null}
      {creating && (
        <Modal
          onClose={() => setCreating(false)}
          busy={createTemplate.isPending}
          label="Новый шаблон"
        >
          <form
            className="dialog"
            onSubmit={(event) => {
              event.preventDefault()
              if (templateName.trim() && csrf) createTemplate.mutate()
            }}
          >
            <h3>Новый шаблон</h3>
            <label>
              Название шаблона
              <input
                value={templateName}
                onChange={(event) => setTemplateName(event.target.value)}
                required
                maxLength={120}
              />
            </label>
            {createTemplate.error && (
              <p role="alert" className="error">
                {renderLibraryError(createTemplate.error)}
              </p>
            )}
            <footer>
              <button
                type="button"
                className="quiet"
                onClick={() => setCreating(false)}
              >
                Отмена
              </button>
              <button type="submit">Создать шаблон</button>
            </footer>
          </form>
        </Modal>
      )}
      {editor && (
        <Suspense fallback={<p role="status">Открываем конструктор…</p>}>
          <GraphEditor
            templateId={editor.templateId}
            initialVersion={editor.version}
            projectId={selectedProjectId}
            onClose={() => setEditor(null)}
            onCreated={(binding) => {
              setEditor(null)
              void client
                .invalidateQueries({ queryKey: ['bindings'] })
                .then(() => setOpenBindingId(binding.id))
            }}
          />
        </Suspense>
      )}
      {openTemplate ? (
        <TemplateVersionsDialog
          template={openTemplate}
          projectId={selectedProjectId}
          onCreated={(binding) => {
            setOpenTemplate(null)
            void client
              .invalidateQueries({ queryKey: ['bindings'] })
              .then(() => setOpenBindingId(binding.id))
          }}
          onClose={() => setOpenTemplate(null)}
          onEdit={(version) => {
            setOpenTemplate(null)
            setEditor({ templateId: openTemplate.id, version })
          }}
        />
      ) : null}
      {openBindingId ? (
        <BindingEditor
          key={openBindingId}
          binding={
            bindingsData.find((item) => item.id === openBindingId) ?? null
          }
          resources={resources}
          onClose={() => setOpenBindingId(null)}
          onSaved={() => {
            setOpenBindingId(null)
            void client.invalidateQueries({ queryKey: ['bindings'] })
          }}
        />
      ) : null}
    </section>
  )
}

interface TemplateCopyDialogProps {
  editAfterCopy?: boolean
  sourceName: string
  isPreset?: boolean
  csrf: string
  pending: boolean
  error: unknown
  onClose: () => void
  onSubmit: (name: string) => void
}

function TemplateCopyDialog({
  editAfterCopy,
  sourceName,
  isPreset = false,
  csrf,
  pending,
  error,
  onClose,
  onSubmit,
}: TemplateCopyDialogProps) {
  const [name, setName] = useState(`${sourceName.slice(0, 112)} - копия`)
  return (
    <Modal onClose={onClose} busy={pending} labelledBy="copy-preset-title">
      <form
        className="dialog"
        onSubmit={(event) => {
          event.preventDefault()
          if (!csrf || pending) return
          onSubmit(name.trim())
        }}
      >
        <header>
          <h3 id="copy-preset-title">
            {editAfterCopy
              ? 'Редактировать шаблон'
              : isPreset
                ? 'Копировать пресет'
                : 'Копировать шаблон'}
          </h3>
          <button type="button" className="quiet" onClick={onClose}>
            ×
          </button>
        </header>
        <p className="hint">
          {isPreset
            ? `Будет создан пользовательский шаблон с графом и настройками пресета «${sourceName}».`
            : `Будет скопирован шаблон «${sourceName}»: сохранённый черновик и последняя опубликованная версия, если она есть.`}{' '}
          Дальнейшие правки относятся к вашей копии.
        </p>
        <label htmlFor="copy-preset-name">Название шаблона</label>
        <input
          id="copy-preset-name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          maxLength={120}
          required
        />
        {error ? (
          <p className="error" role="alert">
            {renderLibraryError(error)}
          </p>
        ) : null}
        <footer>
          <button type="button" className="quiet" onClick={onClose}>
            Отмена
          </button>
          <button type="submit" disabled={pending || !csrf || !name.trim()}>
            {pending
              ? 'Копируем…'
              : editAfterCopy
                ? 'Создать копию и редактировать'
                : 'Копировать'}
          </button>
        </footer>
      </form>
    </Modal>
  )
}

interface TemplateVersionsDialogProps {
  template: PipelineTemplate
  projectId: string | null
  onCreated: (binding: PipelineBinding) => void
  onClose: () => void
  onEdit: (version: PipelineVersion) => void
}

function TemplateVersionsDialog({
  template,
  projectId,
  onCreated,
  onClose,
  onEdit,
}: TemplateVersionsDialogProps) {
  const csrf = useCsrfToken()
  const [name, setName] = useState(template.name.slice(0, 120))
  const create = useMutation({
    mutationFn: (versionId: string) =>
      bindingsApi.create(
        versionId,
        { project_id: projectId!, name: name.trim() },
        csrf,
      ),
    onSuccess: onCreated,
  })
  const versions = useQuery({
    queryKey: ['template_versions', { templateId: template.id }],
    queryFn: () => templatesApi.listVersions(template.id),
  })
  return (
    <Modal
      onClose={onClose}
      busy={create.isPending}
      labelledBy="versions-title"
    >
      <div className="dialog wide">
        <header>
          <h3 id="versions-title">Версии: {template.name}</h3>
          <button type="button" className="quiet" onClick={onClose}>
            ×
          </button>
        </header>
        {projectId ? (
          <>
            <label htmlFor="new-binding-name">Название новой привязки</label>
            <input
              id="new-binding-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              maxLength={120}
            />
          </>
        ) : (
          <p>Выберите проект, чтобы создать привязку.</p>
        )}
        {create.error ? (
          <p role="alert" className="error">
            {renderLibraryError(create.error)}
          </p>
        ) : null}
        {versions.isLoading ? (
          <p role="status">Загружаем версии…</p>
        ) : versions.error ? (
          <p className="error">{renderLibraryError(versions.error)}</p>
        ) : !versions.data?.length ? (
          <p className="panel-empty">
            Версий пока нет. Опубликуйте черновик, чтобы получить неизменяемую
            версию.
          </p>
        ) : (
          <ol className="panel-list">
            {(versions.data as PipelineVersion[]).map((version) => (
              <li key={version.id} className="profile-item">
                <header>
                  <strong>v{version.version_number}</strong>
                  <span className="pill">
                    {version.origin === 'imported' ? 'импорт' : 'локально'}
                  </span>
                </header>
                <span className="meta">
                  <span>
                    {formatDateTime(version.created_at)} · hash{' '}
                    {shortHash(version.execution_hash, 12)}
                  </span>
                  <span className="muted">
                    execution_hash {shortHash(version.execution_hash)}
                  </span>
                </span>
                <details>
                  <summary>Входы и настройки версии</summary>
                  <pre>
                    {JSON.stringify(
                      { inputs: version.inputs, settings: version.settings },
                      null,
                      2,
                    )}
                  </pre>
                </details>
                <button
                  type="button"
                  disabled={
                    !projectId || !csrf || !name.trim() || create.isPending
                  }
                  onClick={() => create.mutate(version.id)}
                >
                  Создать привязку v{version.version_number}
                </button>
                {template.kind !== 'system' && (
                  <button
                    type="button"
                    className="quiet"
                    onClick={() => onEdit(version)}
                  >
                    Открыть v{version.version_number} в конструкторе
                  </button>
                )}
              </li>
            ))}
          </ol>
        )}
      </div>
    </Modal>
  )
}

function renderLibraryError(error: unknown): string {
  if (error instanceof ApiError) return error.body.message
  if (error instanceof Error) return error.message
  return 'Не удалось загрузить данные.'
}
