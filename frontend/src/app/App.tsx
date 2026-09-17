import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ApiError,
  request,
  type Session,
  type SystemStatus,
} from '../api/client'
import { chatsApi, projectsApi, type Project } from '../api/projects'
import { ProjectsPanel } from '../features/projects/ProjectsPanel'
import { ChatsPanel } from '../features/chats/ChatsPanel'
import { ChatView } from '../features/chats/ChatView'
import {
  SettingsView,
  type SettingsSection,
} from '../features/settings/SettingsView'
import { LibraryView } from '../features/bindings/LibraryView'
import { RunsView } from '../features/runs/RunsView'

export function App() {
  const client = useQueryClient()
  const [code, setCode] = useState('')
  const session = useQuery({
    queryKey: ['session'],
    queryFn: () => request<Session>('/auth/session'),
    refetchInterval: 30_000,
  })
  const authExpired =
    session.error instanceof ApiError && session.error.status === 401
  const signedIn = !!session.data && !authExpired
  const system = useQuery({
    queryKey: ['system'],
    queryFn: () => request<SystemStatus>('/system/status'),
    enabled: signedIn,
    refetchInterval: 5_000,
  })
  const pair = useMutation({
    mutationFn: () =>
      request<Session>('/auth/pair', {
        method: 'POST',
        body: JSON.stringify({ code }),
      }),
    onSuccess: (data) => {
      setCode('')
      client.setQueryData(['session'], data)
      void client.invalidateQueries({ queryKey: ['session'] })
    },
  })
  const logout = useMutation({
    mutationFn: () =>
      request<void>(
        '/auth/logout',
        { method: 'POST' },
        session.data?.csrf_token,
      ),
    onSuccess: () => {
      client.clear()
      void client.invalidateQueries({ queryKey: ['session'] })
    },
  })
  const connectionError =
    session.error &&
    !(session.error instanceof ApiError && session.error.status === 401)

  if (!signedIn) {
    return (
      <div className="shell">
        <aside className="sidebar">
          <a className="brand" href="/" aria-label="Agents IDE — главная">
            <span className="brand-mark">A</span> Agents IDE
          </a>
          <span className="section-label">РАБОЧАЯ ОБЛАСТЬ</span>
          <div className="nav-current">
            Обзор <span>01</span>
          </div>
          <div className="sidebar-empty">
            Здесь появятся ваши проекты и чаты.
          </div>
          <div className="sidebar-footer">
            <span className="dot" /> Локальная среда
          </div>
        </aside>
        <div className="workspace">
          <header>
            <span>
              Рабочая область <span className="slash">/</span> Обзор
            </span>
            <span className="version">Каркас · v0.1</span>
          </header>
          <main>
            <p className="eyebrow">AGENTS IDE</p>
            <h1>
              Место для работы
              <br />
              ваших агентов.
            </h1>
            <p className="intro">
              Проекты, диалоги и процессы выполнения — в одной локальной рабочей
              области.
            </p>
            <section className="card pairing" aria-labelledby="pair-title">
              <div className="card-heading">
                <span className="step-number">01</span>
                <h2 id="pair-title">Подключить этот браузер</h2>
              </div>
              <p>
                Получите одноразовый код в терминале на этом компьютере. Он
                действует 5 минут.
              </p>
              <code className="command">agents-ide auth pair-code</code>
              <form
                onSubmit={(event) => {
                  event.preventDefault()
                  pair.mutate()
                }}
              >
                <label htmlFor="pair-code">Код подключения</label>
                <div className="input-row">
                  <input
                    id="pair-code"
                    type="password"
                    value={code}
                    onChange={(event) => setCode(event.target.value)}
                    autoComplete="off"
                    spellCheck={false}
                    required
                    maxLength={128}
                    placeholder="Вставьте код из терминала"
                  />
                  <button disabled={pair.isPending || !code}>
                    {pair.isPending ? 'Подключение…' : 'Подключиться →'}
                  </button>
                </div>
              </form>
              {pair.error ? (
                <p className="error" role="alert">
                  {pair.error.message}
                </p>
              ) : null}
              {connectionError ? (
                <p className="error" role="alert">
                  API недоступен. Запустите локальные службы и повторите
                  попытку. Журнал ошибок:{' '}
                  <code>./scripts/agents-ide.ps1 logs</code>
                </p>
              ) : null}
            </section>
            <section className="next-section">
              <span className="section-label">СЛЕДУЮЩИЙ ЭТАП</span>
              <div className="next-row">
                <h2>От идеи к выполнению</h2>
                <span className="badge">В разработке</span>
              </div>
              <p>
                Добавление проектов, чатов и настроек исполнителей появится на
                следующем этапе.
              </p>
              <div className="future-items">
                <span>01 &nbsp; Проекты</span>
                <span>02 &nbsp; Диалоги</span>
                <span>03 &nbsp; Процессы</span>
              </div>
            </section>
          </main>
          <footer>
            Ваши проекты остаются на вашем компьютере.
            <span>Этапы 0 / 1</span>
          </footer>
        </div>
      </div>
    )
  }

  return (
    <SignedInShell
      system={system.data}
      systemLoading={system.isLoading}
      systemError={
        session.error
          ? describeSystemError(session.error)
          : system.error
            ? describeSystemError(system.error)
            : null
      }
      onLogout={() => logout.mutate()}
      logoutPending={logout.isPending}
    />
  )
}

interface SignedInShellProps {
  system: SystemStatus | undefined
  systemLoading: boolean
  systemError: string | null
  onLogout: () => void
  logoutPending: boolean
}

function SignedInShell({
  system,
  systemLoading,
  systemError,
  onLogout,
  logoutPending,
}: SignedInShellProps) {
  const [selectedProject, setSelectedProject] = useState<Project | null>(null)
  const [selectedChatId, setSelectedChatId] = useState<string | null>(null)
  const [view, setView] = useState<MainView>('chats')
  const [editorTemplateId, setEditorTemplateId] = useState<string | null>(null)
  const [settingsSection, setSettingsSection] =
    useState<SettingsSection>('harnesses')
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const chats = useQuery({
    queryKey: [
      'chats',
      { projectId: selectedProject?.id ?? '', includeArchived: false },
    ],
    queryFn: () => chatsApi.list({ projectId: selectedProject!.id }),
    enabled: Boolean(selectedProject),
  })
  const projects = useQuery({
    queryKey: ['projects', { includeArchived: false }],
    queryFn: () => projectsApi.list({ includeArchived: false }),
    enabled: view === 'library',
  })
  const selectedChat =
    chats.data?.find((chat) => chat.id === selectedChatId) ??
    chats.data?.[0] ??
    null

  return (
    <div className="shell workspace-shell">
      <aside className="sidebar">
        <a className="brand" href="/" aria-label="Agents IDE — главная">
          <span className="brand-mark">A</span> Agents IDE
        </a>
        <span className="section-label">РАБОЧАЯ ОБЛАСТЬ</span>
        <ProjectsPanel
          selectedId={selectedProject?.id ?? null}
          onRenamed={(project) => {
            setSelectedProject((selected) =>
              selected?.id === project.id ? project : selected,
            )
          }}
          onRemoved={(projectId) => {
            if (selectedProject?.id !== projectId) return
            setSelectedProject(null)
            setSelectedChatId(null)
            if (view === 'runs') setView('chats')
          }}
          onSelect={(project) => {
            setSelectedProject(project)
            if (project.id !== selectedProject?.id) setSelectedChatId(null)
            if (view !== 'library') setView('chats')
          }}
        />
        <nav className="sidebar-nav" aria-label="Разделы">
          <button
            type="button"
            className={`quiet nav-button${view === 'chats' ? ' selected' : ''}`}
            aria-pressed={view === 'chats'}
            onClick={() => setView('chats')}
            disabled={!selectedProject}
          >
            Диалоги
          </button>
          <button
            type="button"
            className={`quiet nav-button${view === 'library' ? ' selected' : ''}`}
            aria-pressed={view === 'library'}
            onClick={() => {
              setEditorTemplateId(null)
              setView('library')
            }}
          >
            Шаблоны
          </button>
          <button
            type="button"
            className={`quiet nav-button${view === 'runs' ? ' selected' : ''}`}
            aria-pressed={view === 'runs'}
            onClick={() => setView('runs')}
            disabled={!selectedProject}
          >
            Запуски
          </button>
          <button
            type="button"
            className={`quiet nav-button${view === 'settings' ? ' selected' : ''}`}
            aria-pressed={view === 'settings'}
            onClick={() => setView('settings')}
          >
            Настройки
          </button>
        </nav>
        <div className="sidebar-footer">
          <span className="dot" /> Локальная среда
        </div>
      </aside>
      <aside
        className="subpanel"
        hidden={view === 'settings' || view === 'library' || view === 'runs'}
      >
        <ChatsPanel
          key={selectedProject?.id ?? ''}
          projectId={selectedProject?.id ?? ''}
          selectedId={selectedChat?.id ?? null}
          onSelect={(chat) => setSelectedChatId(chat.id)}
        />
      </aside>
      <div className="workspace">
        <header>
          <span>
            {view === 'settings'
              ? 'Настройки'
              : view === 'library'
                ? 'Шаблоны'
                : view === 'runs'
                  ? selectedProject
                    ? `Запуски · ${selectedProject.name}`
                    : 'Запуски'
                  : selectedProject
                    ? selectedProject.name
                    : 'Обзор'}
            {view === 'chats' && selectedChat ? (
              <>
                <span className="slash">/</span>
                {selectedChat.title}
              </>
            ) : null}
          </span>
          <span className="version">
            {system?.version ? `v${system.version}` : 'Этап 9 · MVP'}
          </span>
        </header>
        <main className="workspace-main">
          {systemError ? (
            <p role="status" className="error">
              Связь со службами: {systemError}. Сохранённый экран остаётся
              доступен.{' '}
              <button
                type="button"
                className="quiet"
                onClick={() => {
                  setSettingsSection('logs')
                  setView('settings')
                }}
              >
                Журнал приложения
              </button>
            </p>
          ) : system && system.worker.status !== 'running' ? (
            <p role="status" className="hint">
              Исполнитель недоступен. Задания ожидают запуска службы.{' '}
              <button
                type="button"
                className="quiet"
                onClick={() => {
                  setSettingsSection('logs')
                  setView('settings')
                }}
              >
                Журнал приложения
              </button>
            </p>
          ) : null}
          {view === 'settings' ? (
            <SettingsView
              section={settingsSection}
              onSectionChange={setSettingsSection}
            />
          ) : view === 'library' ? (
            <LibraryView
              projects={(projects.data ?? []) as Project[]}
              selectedProjectId={selectedProject?.id ?? null}
              initialEditorTemplateId={editorTemplateId}
              onSelectProject={(project) => {
                if (project.id !== selectedProject?.id) setSelectedChatId(null)
                setSelectedProject(project)
              }}
              onOpenChats={() => setView('chats')}
            />
          ) : view === 'runs' ? (
            <RunsView
              key={selectedProject?.id ?? 'runs'}
              project={selectedProject}
              chats={chats.data ?? []}
              selectedChatId={selectedChatId}
              onSelectChat={setSelectedChatId}
            />
          ) : selectedProject ? (
            <ChatView
              key={selectedChat?.id ?? 'empty'}
              projectId={selectedProject.id}
              chat={selectedChat}
              draftText={drafts[selectedChat?.id ?? ''] ?? ''}
              onOpenTemplates={(templateId) => {
                setEditorTemplateId(templateId ?? null)
                setView('library')
              }}
              onDraftChange={(text) => {
                if (selectedChat)
                  setDrafts((previous) => ({
                    ...previous,
                    [selectedChat.id]: text,
                  }))
              }}
              onArchived={(id) =>
                setSelectedChatId((selected) =>
                  selected === id ? null : selected,
                )
              }
            />
          ) : (
            <WelcomePane
              system={system}
              loading={systemLoading}
              error={systemError}
            />
          )}
        </main>
        <footer>
          <span>Откройте диалог проекта и нажмите «Запустить шаблон».</span>
          <button
            type="button"
            className="quiet"
            onClick={onLogout}
            disabled={logoutPending}
          >
            Выйти
          </button>
        </footer>
      </div>
    </div>
  )
}

type MainView = 'chats' | 'library' | 'runs' | 'settings'

interface WelcomePaneProps {
  system: SystemStatus | undefined
  loading: boolean
  error: string | null
}

function WelcomePane({ system, loading, error }: WelcomePaneProps) {
  return (
    <section className="main-pane">
      <header className="main-header">
        <div>
          <span className="eyebrow">ОБЗОР</span>
          <h2>Добро пожаловать в Agents IDE</h2>
          <span className="muted">
            Выберите проект слева, чтобы открыть диалоги и настройки запуска.
          </span>
        </div>
      </header>
      <section className="card" aria-labelledby="system-title">
        <div className="card-heading">
          <span className="step-number">01</span>
          <h2 id="system-title">Состояние служб</h2>
        </div>
        {system && !error ? (
          <>
            <div className="status-grid">
              <Status
                label="API"
                ready={system.api === 'ready'}
                text="Подключён"
              />
              <Status
                label="Хранилище"
                ready={system.database === 'ready'}
                text={system.database === 'ready' ? 'Готово' : 'Недоступно'}
              />
              <Status
                label="Исполнитель"
                ready={system.worker.status === 'running'}
                text={
                  system.worker.status === 'running' ? 'Работает' : 'Не запущен'
                }
              />
            </div>
            <p>
              Исполнитель работает отдельно от браузера. Его состояние
              сохраняется в локальной базе данных.
            </p>
          </>
        ) : loading ? (
          <p role="status">Проверяем службы…</p>
        ) : (
          <p className="error" role="alert">
            {error ?? 'Не удалось получить состояние служб.'}
          </p>
        )}
      </section>
      <section className="next-section">
        <span className="section-label">ЧТО ДАЛЬШЕ</span>
        <div className="next-row">
          <h2>Настройки и запуск</h2>
          <span className="badge">Этап 9</span>
        </div>
        <p>
          В настройках доступны профили, подключения и группы моделей. В
          шаблонах можно скопировать пресет и создать привязку проекта.
        </p>
        <div className="future-items">
          <span>01 &nbsp; Настройки</span>
          <span>02 &nbsp; Группы</span>
          <span>03 &nbsp; Запуск</span>
        </div>
      </section>
    </section>
  )
}

function Status({
  label,
  ready,
  text,
}: {
  label: string
  ready: boolean
  text: string
}) {
  return (
    <div className="service">
      <span>{label}</span>
      <strong>
        <span className={`dot ${ready ? '' : 'offline'}`} />
        {text}
      </strong>
    </div>
  )
}

function describeSystemError(error: unknown): string {
  if (error instanceof ApiError) return error.body.message
  if (error instanceof Error) return error.message
  return 'Не удалось получить состояние служб.'
}
