import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  BookOpen,
  Maximize2,
  MessageCircleQuestion,
  Minimize2,
  MousePointer2,
  RefreshCw,
  RotateCcw,
  Send,
  X,
} from 'lucide-react'
import {
  assistanceApi,
  type AssistanceTarget,
  type AssistanceTurn,
} from '../../api/assistance'
import { useCsrfToken } from '../../app/session'
import { ApiError } from '../../api/client'
import { RunScreen } from '../runs/RunsPanel'
import { AssistanceActions } from './context'
import { AssistanceQuestions } from './AssistanceQuestions'
import { AssistanceGitTool } from './AssistanceGitTool'
import { AssistanceConnectionTool } from './AssistanceConnectionTool'
import { DiagnosticGuide } from './DiagnosticGuide'
import { targetKey } from './targets'
import './assistance.css'

interface Conversation {
  messages: AssistanceTurn[]
  draft: string
  pending?: string
  error?: string
  reset?: boolean
}
const emptyConversation: Conversation = { messages: [], draft: '' }

export function AssistanceProvider({
  children,
  currentTarget,
  onOpenSource,
  onOpenConnections,
}: {
  children: ReactNode
  currentTarget: AssistanceTarget
  onOpenSource: (projectId: string, chatId: string | null) => Promise<void>
  onOpenConnections: () => void
}) {
  const client = useQueryClient()
  const csrf = useCsrfToken()
  const [opened, setOpened] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const [picking, setPicking] = useState(false)
  const [target, setTarget] = useState<AssistanceTarget>(currentTarget)
  const [guideOpen, setGuideOpen] = useState(false)
  const [reviewRun, setReviewRun] = useState<string | null>(null)
  const [toolBusy, setToolBusy] = useState(false)
  const [conversations, setConversations] = useState<
    Record<string, Conversation>
  >({})
  const [modelKey, setModelKey] = useState('')
  const panel = useRef<HTMLElement>(null)
  const composer = useRef<HTMLTextAreaElement>(null)
  const messagesEnd = useRef<HTMLDivElement>(null)
  const launcher = useRef<HTMLButtonElement>(null)
  const hasOpened = useRef(false)
  const requests = useRef(new Map<string, AbortController>())
  const key = targetKey(target)
  const conversation = conversations[key] ?? emptyConversation
  const context = useQuery({
    queryKey: ['assistance-context', target],
    queryFn: () => assistanceApi.context(target),
    enabled: opened,
    refetchInterval: opened ? 10000 : false,
  })
  const models = useQuery({
    queryKey: ['assistance-models'],
    queryFn: assistanceApi.models,
    enabled: opened,
  })
  const selectedModel =
    models.data?.find(
      (model) => `${model.connection_id}:${model.model_id}` === modelKey,
    ) ?? models.data?.[0]
  const suggestions = useQuery({
    queryKey: [
      'assistance-suggestions',
      target,
      selectedModel?.connection_id,
      selectedModel?.model_id,
      context.data?.diagnostic_revision,
    ],
    queryFn: async ({ signal }) => {
      if (!selectedModel || !csrf || !context.data)
        throw new Error('Выберите модель и тему.')
      try {
        return await assistanceApi.suggestions(
          {
            target,
            connection_id: selectedModel.connection_id,
            model_id: selectedModel.model_id,
            diagnostic_revision: context.data.diagnostic_revision,
          },
          csrf,
          signal,
        )
      } catch (error) {
        if (
          error instanceof ApiError &&
          error.body.code === 'assistance_context_changed'
        )
          void client.invalidateQueries({
            queryKey: ['assistance-context', target],
          })
        throw error
      }
    },
    enabled:
      opened &&
      !picking &&
      !!selectedModel &&
      !!csrf &&
      !!context.data &&
      !context.isError,
    staleTime: 5 * 60 * 1000,
    refetchOnWindowFocus: false,
    retry: false,
  })
  const catalog = useQuery({
    queryKey: ['assistance-catalog'],
    queryFn: assistanceApi.catalog,
    enabled: opened && guideOpen,
  })

  function open(next?: AssistanceTarget) {
    if (toolBusy) return
    if (next) setTarget(next)
    else if (!hasOpened.current) setTarget(currentTarget)
    hasOpened.current = true
    setOpened(true)
  }
  function close() {
    if (toolBusy) return
    setPicking(false)
    setOpened(false)
    launcher.current?.focus()
  }
  function updateConversation(id: string, update: Partial<Conversation>) {
    setConversations((previous) => ({
      ...previous,
      [id]: { ...(previous[id] ?? emptyConversation), ...update },
    }))
  }

  function resetConversation() {
    if (toolBusy) return
    requests.current.get(key)?.abort()
    requests.current.delete(key)
    setConversations((previous) => ({
      ...previous,
      [key]: { messages: [], draft: '', reset: true },
    }))
    composer.current?.focus()
  }

  async function refreshDiagnosis() {
    const previousRevision = context.data?.diagnostic_revision
    const refreshed = await context.refetch()
    // A changed revision selects a new query automatically. Otherwise regenerate once.
    if (
      refreshed.data?.diagnostic_revision === previousRevision &&
      selectedModel &&
      csrf
    )
      void suggestions.refetch()
  }

  useEffect(() => {
    const pending = requests.current
    return () => {
      for (const controller of pending.values()) controller.abort()
    }
  }, [])

  useEffect(() => {
    if (opened && !picking) composer.current?.focus()
  }, [opened, picking, key, conversation.reset])

  useEffect(() => {
    if (conversation.messages.length || conversation.pending) {
      const container = messagesEnd.current?.closest<HTMLElement>(
        '[data-assistance-scroll]',
      )
      container?.scrollTo({ top: container.scrollHeight })
    }
  }, [conversation.messages.length, conversation.pending, opened, key])

  useEffect(() => {
    if (!picking) return
    let highlighted: HTMLElement | null = null
    document.body.classList.add('assistance-picking')
    const find = (event: Event) => {
      const element = event.target instanceof Element ? event.target : null
      if (element?.closest('[data-assistance-ui]')) return null
      return element?.closest<HTMLElement>('[data-assistance-target]') ?? null
    }
    const highlight = (event: Event) => {
      const next = find(event)
      if (next === highlighted) return
      highlighted?.classList.remove('assistance-highlight')
      highlighted = next
      highlighted?.classList.add('assistance-highlight')
    }
    const intercept = (event: Event) => {
      if (
        event.target instanceof Element &&
        event.target.closest('[data-assistance-ui]')
      )
        return
      event.preventDefault()
      event.stopPropagation()
      event.stopImmediatePropagation()
      const element = find(event)
      if (event.type === 'click' && element?.dataset.assistanceTarget) {
        setTarget(
          JSON.parse(element.dataset.assistanceTarget) as AssistanceTarget,
        )
        setPicking(false)
      }
    }
    const keyboard = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopImmediatePropagation()
        setPicking(false)
      } else if (
        (event.key === 'Enter' || event.key === ' ') &&
        !(
          event.target instanceof Element &&
          event.target.closest('[data-assistance-ui]')
        )
      ) {
        event.preventDefault()
        event.stopImmediatePropagation()
        const element = find(event)
        if (element?.dataset.assistanceTarget) {
          setTarget(
            JSON.parse(element.dataset.assistanceTarget) as AssistanceTarget,
          )
          setPicking(false)
        }
      }
    }
    document.addEventListener('pointerover', highlight, true)
    document.addEventListener('focusin', highlight, true)
    const blockedEvents = [
      'pointerdown',
      'pointerup',
      'mousedown',
      'mouseup',
      'click',
      'dblclick',
      'auxclick',
      'contextmenu',
    ]
    for (const type of blockedEvents) {
      document.addEventListener(type, intercept, true)
    }
    document.addEventListener('keydown', keyboard, true)
    return () => {
      highlighted?.classList.remove('assistance-highlight')
      document.body.classList.remove('assistance-picking')
      document.removeEventListener('pointerover', highlight, true)
      document.removeEventListener('focusin', highlight, true)
      for (const type of blockedEvents) {
        document.removeEventListener(type, intercept, true)
      }
      document.removeEventListener('keydown', keyboard, true)
    }
  }, [picking])

  async function send() {
    const question = conversation.draft.trim()
    if (
      !question ||
      !csrf ||
      toolBusy ||
      !selectedModel ||
      context.isError ||
      !context.data ||
      requests.current.has(key)
    )
      return
    const controller = new AbortController()
    requests.current.set(key, controller)
    updateConversation(key, {
      pending: question,
      error: undefined,
      reset: false,
    })
    try {
      const answer = await assistanceApi.send(
        {
          target,
          connection_id: selectedModel.connection_id,
          model_id: selectedModel.model_id,
          message: question,
          history: conversation.messages.slice(-12),
        },
        csrf,
        controller.signal,
      )
      if (requests.current.get(key) !== controller || controller.signal.aborted)
        return
      client.setQueryData(['assistance-context', target], answer.context)
      updateConversation(key, {
        pending: undefined,
        draft: '',
        messages: [
          ...conversation.messages,
          { role: 'user', content: question },
          { role: 'assistant', content: answer.content },
        ],
      })
    } catch (error) {
      if (
        requests.current.get(key) === controller &&
        !controller.signal.aborted
      )
        updateConversation(key, {
          pending: undefined,
          error:
            error instanceof Error
              ? error.message
              : 'Не удалось получить ответ.',
        })
    } finally {
      if (requests.current.get(key) === controller) requests.current.delete(key)
    }
  }

  return (
    <AssistanceActions.Provider value={{ open }}>
      {children}
      <button
        ref={launcher}
        type="button"
        className="assistance-launcher"
        data-assistance-ui
        aria-label="ИИ-помощь"
        aria-expanded={opened}
        aria-controls="assistance-panel"
        onClick={() => (opened ? close() : open())}
      >
        <MessageCircleQuestion size={19} aria-hidden="true" /> ИИ-помощь
      </button>
      {opened && (
        <section
          ref={panel}
          id="assistance-panel"
          role="dialog"
          aria-modal="false"
          aria-labelledby="assistance-title"
          data-assistance-ui
          className={`assistance-panel${expanded ? ' expanded' : ''}${picking ? ' picking' : ''}`}
          onKeyDown={(event) => {
            if (event.key === 'Escape' && !picking) {
              event.stopPropagation()
              close()
            }
          }}
        >
          <div className="assistance-heading">
            <div>
              <strong id="assistance-title">ИИ-помощник</strong>
              <span>Разберём, что происходит</span>
            </div>
            <button
              type="button"
              className="quiet"
              aria-label="Сбросить чат"
              title="Очистить переписку и контекст выбранной темы"
              disabled={
                toolBusy ||
                (!conversation.messages.length &&
                  !conversation.draft &&
                  !conversation.pending &&
                  !conversation.error)
              }
              onClick={resetConversation}
            >
              <RotateCcw size={17} aria-hidden="true" />
            </button>
            <button
              className="quiet"
              aria-label={expanded ? 'Уменьшить чат' : 'Развернуть чат'}
              onClick={() => setExpanded(!expanded)}
            >
              {expanded ? <Minimize2 size={17} /> : <Maximize2 size={17} />}
            </button>
            <button
              className="quiet"
              aria-label="Свернуть помощника"
              disabled={toolBusy}
              onClick={close}
            >
              <X size={18} />
            </button>
          </div>
          <div className="assistance-topic">
            <label htmlFor="assistance-topic">Тема вопроса</label>
            <div className="assistance-topic-row">
              <input
                id="assistance-topic"
                readOnly
                value={
                  context.data?.title ??
                  (context.isPending ? 'Определяем тему…' : 'Тема недоступна')
                }
              />
              <button
                className="quiet"
                aria-label="Выбрать блок курсором"
                disabled={toolBusy}
                aria-pressed={picking}
                title="Нажмите, затем кликните по блоку, о котором хотите спросить"
                onClick={() => setPicking(!picking)}
              >
                <MousePointer2 size={19} />
              </button>
              <button
                className="quiet"
                aria-label="Справочник для ИИ"
                aria-expanded={guideOpen}
                onClick={() => setGuideOpen(!guideOpen)}
              >
                <BookOpen size={19} />
              </button>
            </div>
            <button
              className="quiet assistance-current"
              disabled={toolBusy}
              onClick={() => setTarget(currentTarget)}
            >
              Текущий экран
            </button>
          </div>
          {!picking && (
            <>
              <div className="assistance-body" data-assistance-scroll>
                {context.isPending && (
                  <p role="status">Собираем диагностику выбранной зоны…</p>
                )}
                {context.error && (
                  <p className="error" role="alert">
                    Не удалось получить диагностику: {context.error.message}
                    <button
                      className="quiet"
                      onClick={() => void context.refetch()}
                    >
                      Повторить
                    </button>
                  </p>
                )}
                {guideOpen && (
                  <div className="assistance-guide">
                    <strong>
                      Справочник для ИИ · {context.data?.guide.title}
                    </strong>
                    {context.data && (
                      <>
                        <p>{context.data.guide.description}</p>
                        <p>
                          Данные: {context.data.guide.available_data.join('; ')}
                          .
                        </p>
                        <p>
                          Может: {context.data.guide.capabilities.join('; ')}.
                        </p>
                        <p>
                          Изменения:{' '}
                          {context.data.guide.allowed_mutations.length
                            ? context.data.guide.allowed_mutations.join('; ')
                            : 'пока только диагностика и рекомендации'}
                          .
                        </p>
                        <div
                          role="region"
                          aria-label="Сценарии текущего состояния"
                        >
                          <DiagnosticGuide guide={context.data.guide} />
                        </div>
                      </>
                    )}
                    <details>
                      <summary>Все зоны справочника</summary>
                      {catalog.error && (
                        <p className="error">{catalog.error.message}</p>
                      )}
                      {catalog.isPending && <p>Загружаем справочник…</p>}
                      {catalog.data?.map((entry) => (
                        <details key={entry.zone}>
                          <summary>{entry.title}</summary>
                          <p>{entry.description}</p>
                          <DiagnosticGuide guide={entry} />
                        </details>
                      ))}
                    </details>
                  </div>
                )}
                {context.data && (
                  <div className="assistance-diagnostics">
                    <div className="assistance-diagnostics-heading">
                      <strong>Самодиагностика</strong>
                      <button
                        className="quiet"
                        aria-label="Обновить диагностику"
                        disabled={context.isFetching}
                        onClick={() => void refreshDiagnosis()}
                      >
                        <RefreshCw size={14} />
                      </button>
                    </div>
                    <span className="muted">
                      Проверено{' '}
                      {new Date(
                        context.data.collected_at * 1000,
                      ).toLocaleTimeString()}
                    </span>
                    {context.data.worker.status !== 'running' && (
                      <p className="hint">
                        Исполнитель недоступен. Задания могут ожидать запуска
                        службы.
                      </p>
                    )}
                    {!context.data.findings.length && (
                      <p>
                        Активных запусков и запросов внимания в этой зоне не
                        найдено.
                      </p>
                    )}
                    {context.data.findings.map((finding) => (
                      <details
                        key={finding.source_id}
                        open={finding.attention || undefined}
                      >
                        <summary>
                          {finding.attention ? '! ' : ''}
                          {finding.chat_title ?? 'Запуск'} ·{' '}
                          {finding.code ?? finding.state}
                        </summary>
                        <p>{finding.explanation}</p>
                        {finding.node_id && (
                          <p className="muted">Этап: {finding.node_id}</p>
                        )}
                        <p>{finding.next_step}</p>
                        {!!(
                          finding.evidence.git_acceptance as
                            { can_review?: boolean } | undefined
                        )?.can_review && (
                          <button
                            className="quiet"
                            disabled={toolBusy}
                            onClick={() => {
                              setOpened(false)
                              setReviewRun(finding.source_id)
                            }}
                          >
                            Сравнить и принять изменения
                          </button>
                        )}
                        <details>
                          <summary>Данные для ИИ</summary>
                          <pre>{JSON.stringify(finding.evidence, null, 2)}</pre>
                        </details>
                        {target.project_id && (
                          <button
                            className="quiet"
                            onClick={() => {
                              void onOpenSource(
                                target.project_id!,
                                finding.chat_id,
                              ).catch((error: unknown) => {
                                updateConversation(key, {
                                  error:
                                    error instanceof Error
                                      ? error.message
                                      : 'Не удалось открыть диалог.',
                                })
                              })
                              setExpanded(false)
                            }}
                          >
                            Открыть {finding.chat_id ? 'диалог' : 'запуски'}
                          </button>
                        )}
                      </details>
                    ))}
                    {context.data.omitted_findings > 0 && (
                      <p>
                        Ещё состояний: {context.data.omitted_findings}. Выберите
                        конкретный диалог.
                      </p>
                    )}
                  </div>
                )}
                <AssistanceQuestions
                  key={JSON.stringify([
                    key,
                    selectedModel,
                    context.data?.diagnostic_revision,
                    suggestions.dataUpdatedAt,
                  ])}
                  context={context.data}
                  questions={
                    suggestions.data?.diagnostic_revision ===
                    context.data?.diagnostic_revision
                      ? (suggestions.data?.questions ?? [])
                      : []
                  }
                  loading={suggestions.isFetching}
                  error={suggestions.error}
                  hasModel={!!selectedModel}
                  disabled={!!conversation.pending}
                  refreshing={context.isFetching || context.isError}
                  onRefresh={() => void refreshDiagnosis()}
                  onChoose={(question) => {
                    updateConversation(key, { draft: question })
                    composer.current?.focus()
                  }}
                />
                <div
                  className="assistance-messages"
                  role="log"
                  aria-label="Переписка с ИИ"
                  aria-live="polite"
                >
                  {conversation.reset && (
                    <p role="status">
                      Чат сброшен. Следующий вопрос будет отправлен без
                      предыдущей переписки.
                    </p>
                  )}
                  {conversation.messages.map((message, index) => (
                    <div
                      className={`assistance-message ${message.role}`}
                      key={index}
                    >
                      <span>
                        {message.role === 'user' ? 'Вы' : 'ИИ-помощник'}
                      </span>
                      <p>{message.content}</p>
                    </div>
                  ))}
                  {conversation.pending && (
                    <>
                      <div className="assistance-message user">
                        <span>Вы</span>
                        <p>{conversation.pending}</p>
                      </div>
                      <p role="status">ИИ изучает состояние и готовит ответ…</p>
                    </>
                  )}
                  {context.data?.tools?.map((tool) => {
                    const Tool =
                      tool.name === 'refresh_commit_connection_and_resume'
                        ? AssistanceConnectionTool
                        : AssistanceGitTool
                    return (
                      <Tool
                        key={`${key}:${tool.run_id}:${tool.name}`}
                        tool={tool}
                        target={target}
                        csrf={csrf ?? undefined}
                        disabled={
                          !!conversation.pending || toolBusy || context.isError
                        }
                        onBusy={setToolBusy}
                        onResult={(result) => {
                          setConversations((previous) => {
                            const current = previous[key] ?? emptyConversation
                            return {
                              ...previous,
                              [key]: {
                                ...current,
                                reset: false,
                                messages: [
                                  ...current.messages,
                                  {
                                    role: 'user',
                                    content:
                                      result.tool ===
                                      'refresh_commit_connection_and_resume'
                                        ? `Подтверждаю обновление проверенной версии подключения для GitCommit и продолжение запуска в «${tool.title}».`
                                        : result.tool ===
                                            'accept_git_head_and_resume'
                                          ? `Подтверждаю принятие проверенного HEAD и продолжение запуска в «${tool.title}».`
                                          : `Подтверждаю принятие изменений файлов ${(result.accepted_paths ?? []).join(', ')} и продолжение запуска в «${tool.title}».`,
                                  },
                                  {
                                    role: 'assistant',
                                    content: result.content,
                                  },
                                ],
                              },
                            }
                          })
                          void client.invalidateQueries({
                            queryKey: ['assistance-context'],
                          })
                          void client.invalidateQueries({ queryKey: ['runs'] })
                          void client.invalidateQueries({
                            queryKey: ['run', result.run_id],
                          })
                          void client.invalidateQueries({
                            queryKey: ['sidebar_activity'],
                          })
                        }}
                      />
                    )
                  })}
                  <div ref={messagesEnd} />
                </div>
                {conversation.error && (
                  <p className="error" role="alert">
                    {conversation.error}
                  </p>
                )}
              </div>
              <form
                className="assistance-composer"
                onSubmit={(event) => {
                  event.preventDefault()
                  void send()
                }}
              >
                <label htmlFor="assistance-model">Модель</label>
                <select
                  id="assistance-model"
                  disabled={!!conversation.pending || !models.data?.length}
                  value={
                    selectedModel
                      ? `${selectedModel.connection_id}:${selectedModel.model_id}`
                      : ''
                  }
                  onChange={(event) => setModelKey(event.target.value)}
                >
                  {!models.data?.length && (
                    <option value="">
                      {models.isPending
                        ? 'Загружаем модели…'
                        : 'Нет доступных моделей'}
                    </option>
                  )}
                  {models.data?.map((model) => (
                    <option
                      key={`${model.connection_id}:${model.model_id}`}
                      value={`${model.connection_id}:${model.model_id}`}
                    >
                      {model.connection_name} · {model.model_id}
                    </option>
                  ))}
                </select>
                {models.error && (
                  <p className="error" role="alert">
                    {models.error.message}{' '}
                    <button
                      type="button"
                      className="quiet"
                      onClick={() => void models.refetch()}
                    >
                      Повторить
                    </button>
                  </p>
                )}
                {models.data?.length === 0 && (
                  <p className="hint">
                    Добавьте LLM-подключение и модель в настройках.{' '}
                    <button
                      type="button"
                      className="quiet"
                      onClick={onOpenConnections}
                    >
                      Открыть подключения
                    </button>
                  </p>
                )}
                <label className="sr-only" htmlFor="assistance-message">
                  Вопрос помощнику
                </label>
                <textarea
                  ref={composer}
                  id="assistance-message"
                  rows={3}
                  maxLength={4000}
                  placeholder="Что произошло и как продолжить?"
                  value={conversation.draft}
                  disabled={!!conversation.pending || toolBusy}
                  onChange={(event) =>
                    updateConversation(key, { draft: event.target.value })
                  }
                  onKeyDown={(event) => {
                    if (
                      event.key === 'Enter' &&
                      !event.shiftKey &&
                      !event.nativeEvent.isComposing
                    ) {
                      event.preventDefault()
                      void send()
                    }
                  }}
                />
                <div className="assistance-send-row">
                  <span>Состояние зоны отправляется выбранной модели</span>
                  <button
                    type="submit"
                    disabled={
                      !conversation.draft.trim() ||
                      !selectedModel ||
                      !!conversation.pending ||
                      toolBusy ||
                      !csrf ||
                      !context.data ||
                      context.isError
                    }
                  >
                    <Send size={15} aria-hidden="true" /> Спросить
                  </button>
                </div>
              </form>
            </>
          )}
        </section>
      )}
      {reviewRun && (
        <RunScreen
          runId={reviewRun}
          initialResolution
          onClose={() => {
            setReviewRun(null)
            setOpened(true)
            void client.invalidateQueries({ queryKey: ['assistance-context'] })
          }}
        />
      )}
      {picking && (
        <div
          className="assistance-picker-banner"
          role="status"
          data-assistance-ui
        >
          <MousePointer2 size={19} /> Кликните по проекту, диалогу или блоку.
          Esc — отмена.
          <button className="quiet" onClick={() => setPicking(false)}>
            Отмена
          </button>
        </div>
      )}
    </AssistanceActions.Provider>
  )
}
