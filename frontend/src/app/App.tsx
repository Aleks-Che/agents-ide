import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ApiError,
  request,
  type Session,
  type SystemStatus,
} from '../api/client'

export function App() {
  const client = useQueryClient()
  const [code, setCode] = useState('')
  const session = useQuery({
    queryKey: ['session'],
    queryFn: () => request<Session>('/auth/session'),
    refetchInterval: 30_000,
  })
  const signedIn = !!session.data && !session.isError
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
        <div className="sidebar-empty">Здесь появятся ваши проекты и чаты.</div>
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
          {!signedIn ? (
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
              {pair.error && (
                <p className="error" role="alert">
                  {pair.error.message}
                </p>
              )}
              {connectionError && (
                <p className="error" role="alert">
                  API недоступен. Запустите локальные службы и повторите
                  попытку.
                </p>
              )}
            </section>
          ) : (
            <section className="card" aria-labelledby="system-title">
              <div className="card-heading">
                <span className="step-number">01</span>
                <h2 id="system-title">Состояние служб</h2>
                <button
                  className="quiet"
                  onClick={() => logout.mutate()}
                  disabled={logout.isPending}
                >
                  Выйти
                </button>
              </div>
              {system.data && !system.isError ? (
                <>
                  <div className="status-grid">
                    <Status
                      label="API"
                      ready={system.data.api === 'ready'}
                      text="Подключён"
                    />
                    <Status
                      label="Хранилище"
                      ready={system.data.database === 'ready'}
                      text={
                        system.data.database === 'ready'
                          ? 'Готово'
                          : 'Недоступно'
                      }
                    />
                    <Status
                      label="Исполнитель"
                      ready={system.data.worker.status === 'running'}
                      text={
                        system.data.worker.status === 'running'
                          ? 'Работает'
                          : 'Не запущен'
                      }
                    />
                  </div>
                  <p>
                    Исполнитель работает отдельно от браузера. Его состояние
                    сохраняется в локальной базе данных.
                  </p>
                </>
              ) : (
                <p role="status">
                  {system.isError
                    ? 'Не удалось получить состояние служб.'
                    : 'Проверяем службы…'}
                </p>
              )}
              {logout.error && (
                <p className="error" role="alert">
                  {logout.error.message}
                </p>
              )}
            </section>
          )}
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
          Ваши проекты остаются на вашем компьютере.<span>Этапы 0 / 1</span>
        </footer>
      </div>
    </div>
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
