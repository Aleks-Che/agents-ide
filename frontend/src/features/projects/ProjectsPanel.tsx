import { Modal } from '../../app/Modal'
import { EditorError } from '../settings/EditorError'
import { useCallback, useState } from 'react'
import {
  contextMenuHandlers,
  type ContextMenuTarget,
} from '../../app/context_menu'
import { ContextMenu } from '../../app/ContextMenu'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError } from '../../api/client'
import { describeError, projectsApi, type Project } from '../../api/projects'
import {
  probeWorkspace,
  summariseProbe,
  type WorkspaceSummary,
} from '../../api/workspace'
import { basename, formatDateTime, shortHash } from '../../app/format'
import { useCsrfToken } from '../../app/session'
import type { SidebarActivity } from '../../api/activity'
import { ActivityIndicators } from '../../app/ActivityIndicators'

interface ProjectMenuTarget extends ContextMenuTarget {
  project: Project
}

interface ProjectsPanelProps {
  activity: SidebarActivity['projects']
  selectedId: string | null
  onSelect: (project: Project) => void
  onRenamed: (project: Project) => void
  onRemoved: (projectId: string) => void
}

export function ProjectsPanel({
  activity,
  selectedId,
  onSelect,
  onRenamed,
  onRemoved,
}: ProjectsPanelProps) {
  const client = useQueryClient()
  const csrf = useCsrfToken()
  const [createOpen, setCreateOpen] = useState(false)
  const [editing, setEditing] = useState<Project | null>(null)
  const [menu, setMenu] = useState<ProjectMenuTarget | null>(null)
  const closeMenu = useCallback(() => setMenu(null), [])
  const projects = useQuery({
    queryKey: ['projects', { includeArchived: false }],
    queryFn: () => projectsApi.list({ includeArchived: false }),
  })
  const remove = useMutation({
    mutationFn: (project: Project) =>
      projectsApi.archive(
        project.id,
        { archive: true, expected_version: project.version },
        csrf,
      ),
    onSuccess: (removed) => {
      client.setQueryData<Project[]>(
        ['projects', { includeArchived: false }],
        (previous = []) => previous.filter((item) => item.id !== removed.id),
      )
      void client.invalidateQueries({ queryKey: ['projects'] })
      onRemoved(removed.id)
    },
    onError: () => {
      void client.invalidateQueries({ queryKey: ['projects'] })
    },
  })

  return (
    <section className="panel" aria-labelledby="projects-heading">
      <header className="panel-header">
        <span className="section-label" id="projects-heading">
          Проекты
        </span>
        <button
          type="button"
          className="quiet"
          onClick={() => setCreateOpen(true)}
          aria-label="Новый проект"
        >
          + Новый
        </button>
      </header>
      <ProjectsList
        activity={activity}
        projects={projects.data ?? []}
        loading={projects.isLoading}
        error={projects.error ? describeError(projects.error) : null}
        selectedId={selectedId}
        onSelect={onSelect}
        onContextMenu={(target) => {
          if (!remove.isPending) remove.reset()
          setMenu(target)
        }}
        onRetry={() => void projects.refetch()}
      />
      {menu ? (
        <ContextMenu
          target={menu}
          label={`Действия с проектом «${menu.project.name}»`}
          onClose={closeMenu}
          items={[
            {
              label: 'Переименовать',
              onSelect: () => setEditing(menu.project),
            },
            {
              label: 'Удалить из списка',
              onSelect: () => remove.mutate(menu.project),
              disabled: remove.isPending || !csrf,
              danger: true,
              title: 'Файлы проекта на диске сохранятся',
            },
          ]}
        />
      ) : null}
      {remove.isPending ? (
        <p className="panel-empty" role="status">
          Удаляем проект из списка…
        </p>
      ) : null}
      {remove.error ? (
        <p className="panel-empty error" role="alert">
          Не удалось удалить «{remove.variables?.name}» из списка.{' '}
          {remove.error instanceof ApiError &&
          remove.error.body.code === 'project_has_active_runs'
            ? 'Сначала завершите активные запуски проекта.'
            : describeError(remove.error)}
        </p>
      ) : null}
      {editing ? (
        <EditProjectDialog
          project={editing}
          onClose={() => setEditing(null)}
          onSaved={(updated) => {
            setEditing(null)
            client.setQueryData<Project[]>(
              ['projects', { includeArchived: false }],
              (previous = []) =>
                previous.map((item) =>
                  item.id === updated.id ? updated : item,
                ),
            )
            void client.invalidateQueries({ queryKey: ['projects'] })
            onRenamed(updated)
          }}
        />
      ) : null}
      {createOpen ? (
        <CreateProjectDialog
          onClose={() => setCreateOpen(false)}
          onCreated={(created) => {
            setCreateOpen(false)
            void client.invalidateQueries({ queryKey: ['projects'] })
            onSelect(created)
          }}
        />
      ) : null}
    </section>
  )
}

interface ProjectsListProps {
  activity: SidebarActivity['projects']
  projects: Project[]
  loading: boolean
  error: string | null
  selectedId: string | null
  onSelect: (project: Project) => void
  onContextMenu: (target: ProjectMenuTarget) => void
  onRetry: () => void
}

function ProjectsList({
  activity,
  projects,
  loading,
  error,
  selectedId,
  onSelect,
  onContextMenu,
  onRetry,
}: ProjectsListProps) {
  if (loading) {
    return (
      <p className="panel-empty" role="status">
        Загружаем проекты…
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
  if (projects.length === 0) {
    return (
      <p className="panel-empty">
        Проектов пока нет. Нажмите «+ Новый», чтобы добавить рабочий каталог.
      </p>
    )
  }
  return (
    <ul className="panel-list" role="listbox" aria-label="Проекты">
      {projects.map((project) => {
        const selected = project.id === selectedId
        return (
          <li key={project.id}>
            <button
              type="button"
              role="option"
              aria-selected={selected}
              className={`panel-item${selected ? ' selected' : ''}`}
              onClick={() => onSelect(project)}
              {...contextMenuHandlers((target) =>
                onContextMenu({ ...target, project }),
              )}
            >
              <span className="panel-item-title">
                <strong>{project.name}</strong>
                <ActivityIndicators
                  activity={activity?.[project.id]}
                  scope="project"
                />
              </span>
              <span className="muted" title={project.workspace.normalized_path}>
                {basename(project.workspace.normalized_path) ||
                  project.workspace.entered_path}
              </span>
              <span className="meta">
                <WorkspaceBadge project={project} />
                <span>{formatDateTime(project.created_at)}</span>
              </span>
            </button>
          </li>
        )
      })}
    </ul>
  )
}

function WorkspaceBadge({ project }: { project: Project }) {
  const { workspace } = project
  if (workspace.git_root_path) {
    return (
      <span className="pill" aria-label="Git репозиторий">
        Git · {workspace.git_default_branch ?? 'локальный'}
        {workspace.git_dirty ? ' · правки' : ''}
      </span>
    )
  }
  return <span className="pill plain">Каталог</span>
}

interface CreateProjectDialogProps {
  onClose: () => void
  onCreated: (project: Project) => void
}

function CreateProjectDialog({ onClose, onCreated }: CreateProjectDialogProps) {
  const csrf = useCsrfToken()
  const [name, setName] = useState('')
  const [workspacePath, setWorkspacePath] = useState('')
  const probe = useMutation({
    mutationFn: (path: string) => probeWorkspace(path, csrf),
  })
  const create = useMutation({
    mutationFn: () =>
      projectsApi.create(
        { name: name.trim(), workspace_path: workspacePath.trim() },
        csrf,
      ),
    onSuccess: onCreated,
  })
  const submit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    create.mutate()
  }
  const probeMatchesPath = probe.variables === workspacePath.trim()
  const probeSummary =
    probeMatchesPath && probe.data ? summariseProbe(probe.data) : null
  const probeError =
    probeMatchesPath && probe.error ? renderProjectError(probe.error) : null

  return (
    <Modal
      onClose={onClose}
      busy={create.isPending}
      labelledBy="create-project-title"
    >
      <form className="dialog" onSubmit={submit}>
        <header>
          <h3 id="create-project-title">Новый проект</h3>
          <button
            type="button"
            className="quiet"
            aria-label="Закрыть"
            onClick={onClose}
          >
            ×
          </button>
        </header>
        <label htmlFor="project-name">Название</label>
        <input
          id="project-name"
          type="text"
          autoFocus
          required
          maxLength={128}
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
        <label htmlFor="project-path">Путь к рабочему каталогу</label>
        <div className="input-row">
          <input
            id="project-path"
            type="text"
            required
            spellCheck={false}
            value={workspacePath}
            placeholder="C:\work\my-project"
            onChange={(event) => setWorkspacePath(event.target.value)}
          />
          <button
            type="button"
            className="quiet"
            disabled={probe.isPending || !workspacePath.trim()}
            onClick={() => probe.mutate(workspacePath.trim())}
          >
            {probe.isPending ? 'Проверяем…' : 'Проверить'}
          </button>
        </div>
        <p className="hint">
          Путь должен существовать на локальном NTFS-томе и быть доступным
          приложению. Сетевые диски и UNC не поддерживаются.
        </p>
        <ProbeSummary summary={probeSummary} error={probeError} />
        {create.error ? (
          <p className="error" role="alert">
            {renderProjectError(create.error)}
          </p>
        ) : null}
        <footer>
          <button type="button" className="quiet" onClick={onClose}>
            Отмена
          </button>
          <button
            type="submit"
            disabled={
              create.isPending || !name.trim() || !workspacePath.trim() || !csrf
            }
          >
            {create.isPending ? 'Создаём…' : 'Создать'}
          </button>
        </footer>
      </form>
    </Modal>
  )
}

function ProbeSummary({
  summary,
  error,
}: {
  summary: WorkspaceSummary | null
  error: string | null
}) {
  if (error) {
    return (
      <p className="error" role="alert">
        {error}
      </p>
    )
  }
  if (!summary) {
    return null
  }
  return (
    <div className="probe-summary" aria-live="polite">
      <span className="muted">Нормализованный путь</span>
      <code title={summary.normalizedPath}>{summary.normalizedPath}</code>
      {summary.isGitRepo ? (
        <>
          <span className="muted">
            Git · {summary.gitDefaultBranch ?? 'локальный'}
            {summary.gitDirty ? ' · есть правки' : ''}
          </span>
          <span className="muted">HEAD {shortHash(summary.gitHeadSha, 7)}</span>
        </>
      ) : (
        <span className="muted">Каталог без Git</span>
      )}
    </div>
  )
}

function renderProjectError(error: unknown): string {
  if (error instanceof ApiError) {
    return error.body.message
  }
  return describeError(error)
}

function EditProjectDialog({
  project: initial,
  onClose,
  onSaved,
}: {
  project: Project
  onClose: () => void
  onSaved: (project: Project) => void
}) {
  const csrf = useCsrfToken()
  const [project, setProject] = useState(initial)
  const [name, setName] = useState(initial.name)
  const save = useMutation({
    mutationFn: () =>
      projectsApi.update(
        project.id,
        { name: name.trim(), expected_version: project.version },
        csrf,
      ),
    onSuccess: onSaved,
  })
  const reload = useMutation({
    mutationFn: () => projectsApi.get(project.id),
    onSuccess: (updated) => {
      setProject(updated)
      setName(updated.name)
      save.reset()
    },
  })
  return (
    <Modal
      onClose={onClose}
      busy={save.isPending || reload.isPending}
      labelledBy="project-edit-title"
    >
      <form
        className="dialog"
        onSubmit={(event) => {
          event.preventDefault()
          if (name.trim() && !save.isPending) save.mutate()
        }}
      >
        <header>
          <h3 id="project-edit-title">Переименовать проект</h3>
          <button
            type="button"
            className="quiet"
            aria-label="Закрыть"
            onClick={onClose}
          >
            ×
          </button>
        </header>
        <label htmlFor="project-edit-name">Название</label>
        <input
          id="project-edit-name"
          required
          autoFocus
          maxLength={128}
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
        <EditorError
          error={save.error ?? reload.error}
          onReload={() => reload.mutate()}
        />
        <footer>
          <button type="button" className="quiet" onClick={onClose}>
            Отмена
          </button>
          <button disabled={!csrf || !name.trim()}>Сохранить</button>
        </footer>
      </form>
    </Modal>
  )
}
