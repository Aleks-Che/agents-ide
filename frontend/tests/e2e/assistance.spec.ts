import { test, expect, pair, api, workspace } from './support'
import type { AssistanceContext } from '../../src/api/assistance'

test('math-portal warning can be selected and diagnosed in the AI popup', async ({
  page,
}) => {
  await pair(page)
  const project = await api(page, 'POST', '/projects', {
    name: 'math-portal',
    workspace_path: workspace(),
  })
  const other = await api(page, 'POST', '/projects', {
    name: 'Другой проект',
    workspace_path: workspace(),
  })
  const chat = await api(page, 'POST', `/projects/${project.id}/chats`, {
    title: 'Фронтенд',
  })
  await page.route('**/api/sidebar/activity', (route) =>
    route.fulfill({
      json: {
        projects: { [project.id]: { running: false, attention: true } },
        chats: { [chat.id]: { running: false, attention: true } },
      },
    }),
  )
  await page.route('**/api/assistance/models', (route) =>
    route.fulfill({
      json: [
        {
          connection_id: 'model-connection',
          connection_name: 'Тестовое подключение',
          model_id: 'diagnostic-model',
        },
      ],
    }),
  )
  let latestContext: AssistanceContext
  let completed = false
  await page.route('**/api/assistance/context?*', (route) => {
    const params = new URL(route.request().url()).searchParams
    const isChat = params.get('zone') === 'chat'
    const selected = params.get('project_id') === project.id
    latestContext = {
      target: {
        zone: isChat ? 'chat' : 'project',
        project_id: params.get('project_id'),
        chat_id: params.get('chat_id'),
      },
      title: selected
        ? `math-portal${isChat ? ' — Фронтенд' : ''}`
        : 'Другой проект',
      collected_at: Date.now() / 1000,
      diagnostic_revision: (completed
        ? 'd'
        : selected
          ? isChat
            ? 'c'
            : 'a'
          : 'b'
      ).repeat(64),
      worker: { status: 'running' },
      omitted_findings: 0,
      guide: {
        zone: isChat ? 'chat' : 'project',
        title: isChat ? 'Диалог проекта' : 'Проект',
        description: 'Значок ! указывает на ошибку или запрос.',
        available_data: ['Причина ожидания'],
        capabilities: ['Объяснить состояние'],
        allowed_mutations: [],
        questions: [],
        diagnostic_rules: ['Текущая диагностика важнее старых ответов чата.'],
        diagnostic_cases: [
          {
            id: 'git_guard',
            title: 'Git остановлен защитной проверкой',
            signals: ['external_change_detected'],
            meaning: 'Рабочая область не прошла проверку условий Git-этапа.',
            checks: [
              'Сопоставить защищённые пути с нужными изменениями пользователя.',
            ],
            limitations: [
              'Обычный git diff не показывает все защищённые игнорируемые файлы.',
            ],
          },
        ],
      },
      questions: selected
        ? [
            'Почему выполнение остановилось перед Git-коммитом и что нужно проверить?',
          ]
        : [],
      findings:
        selected && !completed
          ? [
              {
                source_id: 'blocked-run',
                source_kind: 'run',
                chat_id: chat.id,
                chat_title: 'Фронтенд',
                state: 'waiting_input',
                attention: true,
                node_id: 'gitcommit_1',
                code: 'external_change_detected',
                explanation:
                  'Изменены защищённые файлы вне разрешённого списка.',
                evidence: {
                  error_message: 'Files outside the allowlist changed',
                  allowed_actions: ['resolve', 'resume'],
                },
                next_step: 'Проверьте git status и git diff.',
                question:
                  'Почему выполнение остановилось перед Git-коммитом и что нужно проверить?',
              },
            ]
          : [],
    }
    return route.fulfill({ json: latestContext })
  })
  const suggestionRequests: unknown[] = []
  let releaseSuggestions: (() => void) | undefined
  await page.route('**/api/assistance/suggestions', async (route) => {
    const payload = route.request().postDataJSON()
    suggestionRequests.push(payload)
    if (
      payload.target.project_id === project.id &&
      !payload.target.chat_id &&
      !completed
    )
      await new Promise<void>((resolve) => {
        releaseSuggestions = resolve
      })
    return route.fulfill({
      json: {
        questions: completed
          ? [
              'Что проверить после завершения запуска?',
              'Какие результаты доступны в этом диалоге?',
            ]
          : payload.target.project_id !== project.id
            ? [
                'Как начать работу в выбранном проекте?',
                'Что настроить для первого запуска?',
              ]
            : [
                'Какие изменения README.md мешают коммиту в этом диалоге?',
                'Как принять проверенные изменения через форму восстановления?',
              ],
        diagnostic_revision: payload.diagnostic_revision,
        generated_at: Date.now() / 1000,
      },
    })
  })
  const requests: Array<{
    target: { chat_id?: string }
    message: string
    history: unknown[]
  }> = []
  await page.route('**/api/assistance/messages', async (route) => {
    requests.push(route.request().postDataJSON())
    await route.fulfill({
      json: {
        content:
          'Git-коммит остановлен: изменены защищённые файлы. Проверьте README.md.',
        context: latestContext,
        model: {
          connection_id: 'model-connection',
          connection_name: 'Тестовое подключение',
          model_id: 'diagnostic-model',
        },
      },
    })
  })
  await page.reload()
  const otherOption = page.getByRole('option', { name: new RegExp(other.name) })
  const projectOption = page.getByRole('option', { name: /math-portal/ })
  await otherOption.click()
  await page.getByRole('button', { name: 'ИИ-помощь', exact: true }).click()
  const assistant = page.getByRole('dialog', { name: 'ИИ-помощник' })
  await assistant.getByRole('button', { name: 'Выбрать блок курсором' }).click()
  const warning = projectOption.getByRole('status', {
    name: 'В проекте требуется внимание: ошибка или запрос',
  })
  await warning.hover()
  await expect(projectOption).toHaveClass(/assistance-highlight/)
  await warning.click()
  await expect(assistant.getByLabel('Тема вопроса')).toHaveValue('math-portal')
  await expect(otherOption).toHaveAttribute('aria-selected', 'true')
  await expect(projectOption).toHaveAttribute('aria-selected', 'false')
  await expect(
    assistant.getByText('Изменены защищённые файлы вне разрешённого списка.', {
      exact: true,
    }),
  ).toBeVisible()
  const primaryQuestion =
    'Объясни и предложи решение по существующей проблеме: Фронтенд: Изменены защищённые файлы вне разрешённого списка. А конкретнее: этап gitcommit_1, external_change_detected. Область проекта: math-portal.'
  const primary = assistant.getByRole('button', {
    name: primaryQuestion,
    exact: true,
  })
  await expect(primary).toBeVisible()
  await expect(primary).toBeEnabled()
  const dots = assistant.getByRole('button', {
    name: 'Генерируем дополнительные вопросы',
    exact: true,
  })
  await expect(dots).toBeDisabled()
  await expect(dots).toHaveAttribute('aria-busy', 'true')
  const dotElements = dots.locator('.assistance-question-dots span')
  await expect(dotElements).toHaveCount(3)
  await expect(dotElements.nth(0)).toHaveCSS(
    'animation-name',
    'assistance-question-bounce',
  )
  await expect(dotElements.nth(1)).toHaveCSS('animation-delay', '0.15s')
  await expect(dotElements.nth(2)).toHaveCSS('animation-delay', '0.3s')
  await primary.click()
  await expect(assistant.getByLabel('Вопрос помощнику')).toHaveValue(
    primaryQuestion,
  )
  await expect.poll(() => !!releaseSuggestions).toBe(true)
  releaseSuggestions!()
  const more = assistant.getByRole('button', {
    name: 'Показать дополнительные вопросы',
    exact: true,
  })
  await expect(more).toBeEnabled()
  await expect(
    more.locator('.assistance-question-dots span').first(),
  ).toHaveCSS('animation-name', 'none')
  await expect(
    assistant.getByRole('button', {
      name: 'Какие изменения README.md мешают коммиту в этом диалоге?',
    }),
  ).toHaveCount(0)
  await more.click()
  await expect(
    assistant.getByRole('button', {
      name: 'Какие изменения README.md мешают коммиту в этом диалоге?',
    }),
  ).toBeVisible()
  await assistant
    .getByRole('button', { name: 'Скрыть дополнительные вопросы', exact: true })
    .click()
  await expect(
    assistant.getByRole('button', {
      name: 'Какие изменения README.md мешают коммиту в этом диалоге?',
    }),
  ).toHaveCount(0)
  await assistant.getByRole('button', { name: 'Справочник для ИИ' }).click()
  await expect(
    assistant.getByText('Изменения: пока только диагностика и рекомендации.'),
  ).toBeVisible()
  const currentGuide = assistant.getByRole('region', {
    name: 'Сценарии текущего состояния',
  })
  await currentGuide
    .getByText('Git остановлен защитной проверкой', { exact: true })
    .click()
  await expect(
    currentGuide.getByText('Порядок проверок', { exact: true }),
  ).toBeVisible()
  await expect(
    currentGuide.getByText(
      'Обычный git diff не показывает все защищённые игнорируемые файлы.',
    ),
  ).toBeVisible()
  await assistant.getByRole('button', { name: 'Справочник для ИИ' }).click()
  await projectOption.click()
  const chatOption = page.getByRole('option', { name: /Фронтенд/ })
  await assistant.getByRole('button', { name: 'Выбрать блок курсором' }).click()
  await chatOption.getByRole('status').click()
  await expect(assistant.getByLabel('Тема вопроса')).toHaveValue(
    'math-portal — Фронтенд',
  )
  await assistant
    .getByRole('button', {
      name: 'Показать дополнительные вопросы',
      exact: true,
    })
    .click()
  await assistant
    .getByRole('button', {
      name: 'Какие изменения README.md мешают коммиту в этом диалоге?',
    })
    .click()
  await assistant.getByRole('button', { name: 'Спросить', exact: true }).click()
  await expect(assistant.getByRole('log')).toContainText('Проверьте README.md.')
  expect(requests).toHaveLength(1)
  expect(requests[0].target.chat_id).toBe(chat.id)
  expect(requests[0].message).toBe(
    'Какие изменения README.md мешают коммиту в этом диалоге?',
  )
  const generatedBeforeReopen = suggestionRequests.length
  await assistant.getByRole('button', { name: 'Развернуть чат' }).click()
  await expect(assistant).toHaveClass(/expanded/)
  await assistant.getByRole('button', { name: 'Свернуть помощника' }).click()
  await page.getByRole('button', { name: 'ИИ-помощь', exact: true }).click()
  await expect(assistant.getByRole('log')).toContainText('Проверьте README.md.')
  expect(suggestionRequests).toHaveLength(generatedBeforeReopen)
  await assistant.getByLabel('Вопрос помощнику').fill('Что делать дальше?')
  await assistant.getByLabel('Вопрос помощнику').press('Enter')
  await expect.poll(() => requests.length).toBe(2)
  expect(requests[1].history).toHaveLength(2)
  await expect(assistant.locator('.assistance-message')).toHaveCount(4)
  const selectedModel = await assistant.getByLabel('Модель').inputValue()
  await assistant.getByRole('button', { name: 'Сбросить чат' }).click()
  await expect(assistant.locator('.assistance-message')).toHaveCount(0)
  await expect(assistant.getByLabel('Вопрос помощнику')).toHaveValue('')
  await expect(assistant.getByLabel('Тема вопроса')).toHaveValue(
    'math-portal — Фронтенд',
  )
  await expect(assistant.getByLabel('Модель')).toHaveValue(selectedModel)
  await assistant.getByLabel('Вопрос помощнику').fill('Новая диагностика')
  await assistant.getByRole('button', { name: 'Спросить', exact: true }).click()
  await expect.poll(() => requests.length).toBe(3)
  expect(requests[2].history).toEqual([])
  await expect(assistant.locator('.assistance-message')).toHaveCount(2)
  // Switching scope keeps separate conversations; cancellation restores normal clicks.
  await assistant.getByRole('button', { name: 'Выбрать блок курсором' }).click()
  await page.keyboard.press('Escape')
  await expect(page.locator('.assistance-picker-banner')).toHaveCount(0)
  await assistant.getByRole('button', { name: 'Выбрать блок курсором' }).click()
  await otherOption.click()
  await expect(assistant.getByRole('log')).not.toContainText('README.md')
  await expect(
    assistant.getByRole('button', {
      name: /^Объясни и предложи решение по существующей проблеме/,
    }),
  ).toHaveCount(0)
  await assistant
    .getByRole('button', {
      name: 'Показать дополнительные вопросы',
      exact: true,
    })
    .click()
  await expect(
    assistant.getByRole('button', {
      name: 'Как начать работу в выбранном проекте?',
    }),
  ).toBeVisible()
  await expect(
    assistant.getByRole('button', {
      name: 'Какие изменения README.md мешают коммиту в этом диалоге?',
    }),
  ).toHaveCount(0)
  await assistant.getByLabel('Вопрос помощнику').fill('Черновик другой темы')
  await assistant.getByRole('button', { name: 'Сбросить чат' }).click()
  await expect(assistant.getByLabel('Вопрос помощнику')).toHaveValue('')
  await assistant.getByRole('button', { name: 'Свернуть помощника' }).click()
  await page
    .getByRole('button', { name: 'Спросить ИИ об этом диалоге' })
    .click()
  await expect(assistant.getByLabel('Тема вопроса')).toHaveValue(
    'math-portal — Фронтенд',
  )
  await expect(assistant.getByRole('log')).toContainText('Проверьте README.md.')
  await assistant
    .getByRole('button', {
      name: 'Показать дополнительные вопросы',
      exact: true,
    })
    .click()
  await expect(
    assistant.getByRole('button', {
      name: 'Какие изменения README.md мешают коммиту в этом диалоге?',
    }),
  ).toBeVisible()
  // Routine context polling keeps the model result; a meaningful state change regenerates it.
  const beforePoll = suggestionRequests.length
  await page.clock.install()
  const poll = page.waitForResponse((response) =>
    response.url().includes('/assistance/context?'),
  )
  await page.clock.fastForward(11000)
  await poll
  expect(suggestionRequests).toHaveLength(beforePoll)
  completed = true
  await page.clock.fastForward(11000)
  await expect(
    assistant.getByRole('button', {
      name: /^Объясни и предложи решение по существующей проблеме/,
    }),
  ).toHaveCount(0)
  await assistant
    .getByRole('button', {
      name: 'Показать дополнительные вопросы',
      exact: true,
    })
    .click()
  await expect(
    assistant.getByRole('button', {
      name: 'Что проверить после завершения запуска?',
    }),
  ).toBeVisible()
  expect(suggestionRequests).toHaveLength(beforePoll + 1)
  await expect(
    assistant.getByRole('button', {
      name: 'Какие изменения README.md мешают коммиту в этом диалоге?',
    }),
  ).toHaveCount(0)
  await assistant
    .getByRole('button', { name: 'Сгенерировать вопросы заново' })
    .click()
  await expect.poll(() => suggestionRequests.length).toBe(beforePoll + 2)
  await page.screenshot({ path: '../.local/assistance-desktop.png' })
})

test('diagnosis remains available without a model and fits a small screen', async ({
  page,
}) => {
  await pair(page)
  await page.route('**/api/assistance/models', (route) =>
    route.fulfill({ json: [] }),
  )
  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('button', { name: 'ИИ-помощь', exact: true }).click()
  const assistant = page.getByRole('dialog', { name: 'ИИ-помощник' })
  await expect(
    assistant.getByText('Самодиагностика', { exact: true }),
  ).toBeVisible()
  await assistant.getByLabel('Вопрос помощнику').fill('Что произошло?')
  await expect(
    assistant.getByRole('button', { name: 'Спросить', exact: true }),
  ).toBeDisabled()
  await expect(
    assistant.getByRole('button', { name: 'Открыть подключения' }),
  ).toBeVisible()
  const bounds = await assistant.boundingBox()
  expect(bounds!.x).toBeGreaterThanOrEqual(0)
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390)
  expect(bounds!.y).toBeGreaterThanOrEqual(0)
  await page.screenshot({ path: '../.local/assistance-mobile.png' })
})

test('reset ignores late replies and keeps a new request independent', async ({
  page,
}) => {
  // Simulate a transport that finishes even after cancellation, as a server may do.
  await page.addInitScript(() => {
    const originalFetch = window.fetch.bind(window)
    window.fetch = (input, init) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.href
            : input.url
      return originalFetch(
        input,
        url.endsWith('/api/assistance/messages')
          ? { ...init, signal: undefined }
          : init,
      )
    }
  })
  await pair(page)
  const context = await (
    await page.request.get('/api/assistance/context?zone=application')
  ).json()
  const model = {
    connection_id: 'model-connection',
    connection_name: 'Тест',
    model_id: 'test-model',
  }
  await page.route('**/api/assistance/models', (route) =>
    route.fulfill({ json: [model] }),
  )
  const requests: Array<{ message: string; history: unknown[] }> = []
  const release = new Map<string, () => void>()
  await page.route('**/api/assistance/messages', async (route) => {
    const payload = route.request().postDataJSON()
    requests.push(payload)
    await new Promise<void>((resolve) => release.set(payload.message, resolve))
    await route.fulfill({
      json: { content: `Ответ: ${payload.message}`, context, model },
    })
  })
  await page.getByRole('button', { name: 'ИИ-помощь', exact: true }).click()
  const assistant = page.getByRole('dialog', { name: 'ИИ-помощник' })
  const composer = assistant.getByLabel('Вопрос помощнику')
  await composer.fill('Старый вопрос')
  await assistant.getByRole('button', { name: 'Спросить', exact: true }).click()
  await expect.poll(() => release.has('Старый вопрос')).toBe(true)
  await assistant.getByRole('button', { name: 'Сбросить чат' }).click()
  await expect(composer).toBeEnabled()
  await expect(composer).toBeFocused()
  await composer.fill('Новый вопрос')
  await assistant.getByRole('button', { name: 'Спросить', exact: true }).click()
  await expect.poll(() => release.has('Новый вопрос')).toBe(true)
  const oldResponse = page.waitForResponse(
    (response) =>
      response.url().endsWith('/api/assistance/messages') &&
      response.request().postDataJSON().message === 'Старый вопрос',
  )
  release.get('Старый вопрос')!()
  await (await oldResponse).finished()
  await page.evaluate(() => new Promise(requestAnimationFrame))
  await expect(composer).toBeDisabled()
  await expect(assistant.getByRole('log')).not.toContainText('Старый вопрос')
  // The old request's finally handler must not remove the new request's lock.
  await assistant
    .locator('form')
    .evaluate((form) => (form as HTMLFormElement).requestSubmit())
  const newResponse = page.waitForResponse(
    (response) =>
      response.url().endsWith('/api/assistance/messages') &&
      response.request().postDataJSON().message === 'Новый вопрос',
  )
  release.get('Новый вопрос')!()
  await (await newResponse).finished()
  await expect(assistant.locator('.assistance-message')).toHaveCount(2)
  await expect(assistant.getByRole('log')).toContainText('Ответ: Новый вопрос')
  await expect(assistant.getByRole('log')).not.toContainText('Старый вопрос')
  expect(requests).toHaveLength(2)
  expect(requests[1].history).toEqual([])
})
