import { createServer } from 'node:http'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import path from 'node:path'
import { test, expect, pair, api, workspace } from './support'

const document = (text: string, questions: unknown[] = []) => ({
  body_text: text,
  steps: [
    {
      title: 'Implement feature',
      acceptance_criteria: ['Tests demonstrate behavior'],
    },
  ],
  questions,
})
const questions = [
  {
    id: 'db',
    kind: 'single',
    prompt: 'Database?',
    options: [
      { id: 'a', label: 'SQLite' },
      { id: 'b', label: 'Postgres' },
    ],
  },
  {
    id: 'checks',
    kind: 'multi',
    prompt: 'Which checks?',
    options: [
      { id: 'a', label: 'Unit' },
      { id: 'b', label: 'Browser' },
    ],
  },
  { id: 'constraint', kind: 'text', prompt: 'Constraints?' },
]

test('Council recovers creation, reviews complete plan, confirms revision and supplies Run', async ({
  page,
}) => {
  test.setTimeout(75_000)
  page.setDefaultTimeout(10_000)
  const calls: { model: string; messages: unknown[] }[] = []
  const server = createServer(async (req, res) => {
    let raw = ''
    for await (const chunk of req) raw += chunk
    const body = JSON.parse(raw)
    calls.push(body)
    const content = document(
      body.model === 'merge'
        ? 'Unified visible plan'
        : `Draft for ${body.model}`,
      body.model === 'merge' ? questions : [],
    )
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end(
      JSON.stringify({
        choices: [
          { message: { role: 'assistant', content: JSON.stringify(content) } },
        ],
        usage: { total_tokens: 9 },
      }),
    )
  })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  try {
    await pair(page)
    const address = server.address() as { port: number }
    const project = await api(page, 'POST', '/projects', {
      name: 'Council review',
      workspace_path: workspace(),
    })
    const chat = await api(page, 'POST', `/projects/${project.id}/chats`, {
      title: 'Planning chat',
    })
    const connection = await api(page, 'POST', '/connections', {
      name: 'Local council provider',
      base_url: `http://127.0.0.1:${address.port}/v1`,
    })
    const group = await api(page, 'POST', '/model_groups/llm', {
      name: 'Council group',
      members: [{ model_id: 'first', provider_connection_id: connection.id }],
    })
    const template = await api(page, 'POST', '/templates', {
      name: 'Council consumer',
    })
    const version = await api(
      page,
      'POST',
      `/templates/${template.id}/versions`,
      {
        graph: {
          nodes: [
            { id: 's', type: 'Start' },
            {
              id: 'p',
              type: 'PlanControl',
              config: { operation: 'select_next' },
            },
            { id: 'e', type: 'End' },
          ],
          edges: [
            { id: 'a', from: 's', to: 'p' },
            { id: 'b', from: 'p', to: 'e' },
          ],
        },
      },
    )
    const binding = await api(
      page,
      'POST',
      `/versions/${version.id}/bindings`,
      {
        project_id: project.id,
        name: 'Confirmed plan',
      },
    )
    await page.reload()
    await page.getByRole('option', { name: /Council review/ }).click()
    await page
      .getByRole('button', { name: 'Составить план несколькими моделями' })
      .click()
    let dialog = page.getByRole('dialog')
    await dialog
      .getByLabel('Текст задачи')
      .fill('Make a carefully reviewed change')
    await dialog
      .getByLabel('Контекст для всех участников')
      .fill('Shared context marker')
    await dialog.getByLabel('Число участников').selectOption('2')
    const first = dialog.getByRole('group', { name: 'Участник 1', exact: true })
    await first.getByLabel('Тип выбора').selectOption('group')
    await first
      .getByRole('combobox', { name: 'Группа', exact: true })
      .selectOption(group.id)
    for (const [name, model] of [
      ['Участник 2', 'second'],
      ['Объединяющий', 'merge'],
    ]) {
      const member = dialog.getByRole('group', { name, exact: true })
      await member.getByLabel('Модель', { exact: true }).fill(model)
      await member.getByLabel('Подключение').selectOption(connection.id)
    }
    let originalKey = ''
    await page.route('**/api/planning_jobs', async (route) => {
      if (route.request().method() !== 'POST') return route.continue()
      originalKey = route.request().postDataJSON().idempotency_key
      const response = await route.fetch()
      expect(response.status()).toBe(201)
      await route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({
          code: 'connection_error',
          message: 'Reply lost',
          retryable: true,
        }),
      })
    })
    await dialog
      .getByRole('button', { name: 'Составить план', exact: true })
      .click()
    await expect(dialog.getByRole('alert')).toContainText('Reply lost')
    await page.unroute('**/api/planning_jobs')
    await page.reload()
    await page.getByRole('option', { name: /Council review/ }).click()
    await page
      .getByRole('button', { name: 'Составить план несколькими моделями' })
      .click()
    const created = page.waitForResponse(
      (r) =>
        r.url().endsWith('/api/planning_jobs') &&
        r.request().method() === 'POST',
    )
    await page.getByRole('button', { name: 'Повторить создание' }).click()
    const response = await created
    expect(response.request().postDataJSON().idempotency_key).toBe(originalKey)
    const job = await response.json()
    const jobs = await api(
      page,
      'GET',
      `/planning_jobs?chat_id=${chat.id}&include_completed=true`,
    )
    expect(jobs).toHaveLength(1)
    const python = path.resolve(
      '../backend/.venv',
      process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
    )
    const result = await promisify(execFile)(
      python,
      [
        path.resolve('tests/e2e/dispatch_council.py'),
        process.env.AGENTS_IDE_E2E_DATA_DIR!,
        job.id,
      ],
      { windowsHide: true, timeout: 20_000 },
    )
    expect(result.stdout).toContain('needs_answers')
    dialog = page.getByRole('dialog', { name: 'План совета моделей' })
    await expect(
      dialog.getByText('Unified visible plan', { exact: true }),
    ).toBeVisible()
    expect(calls.map((c) => c.model)).toEqual(['first', 'second', 'merge'])
    expect(JSON.stringify(calls[2].messages)).toContain('Draft for first')
    expect(JSON.stringify(calls[2].messages)).toContain('Draft for second')
    await dialog.getByText('first · принят', { exact: false }).click()
    await expect(
      dialog.getByText('Draft for first', { exact: false }).last(),
    ).toBeVisible()
    await dialog
      .getByRole('combobox', { name: 'Database?', exact: true })
      .selectOption('a')
    await dialog
      .getByRole('listbox', { name: 'Which checks?', exact: true })
      .selectOption(['a', 'b'])
    await dialog
      .getByLabel('Constraints?', { exact: true })
      .fill('Offline only')
    await dialog
      .getByRole('button', { name: 'Сохранить ответы и правки' })
      .click()
    await expect(
      dialog.getByRole('heading', { name: /Ревизия 2/ }),
    ).toBeVisible()
    await expect(dialog.locator('.council-plan')).toContainText('Offline only')
    await dialog.getByLabel('Уточнить план вручную').check()
    const edited = document('Manually checked plan with offline constraints')
    await dialog.getByLabel('Полный план JSON').fill(JSON.stringify(edited))
    await expect(
      dialog.getByRole('button', { name: 'Подтвердить план' }),
    ).toBeDisabled()
    await dialog
      .getByRole('button', { name: 'Сохранить ответы и правки' })
      .click()
    await expect(
      dialog.getByRole('heading', { name: /Ревизия 3/ }),
    ).toBeVisible()
    await page.setViewportSize({ width: 390, height: 844 })
    await expect
      .poll(() => dialog.evaluate((e) => e.scrollWidth <= e.clientWidth + 1))
      .toBe(true)
    await page.screenshot({
      path: '../.local/council-review-mobile.png',
      fullPage: true,
    })
    await dialog.getByRole('button', { name: 'Подтвердить план' }).click()
    await expect(
      dialog.getByRole('heading', { name: /Ревизия 3 .*подтверждена/ }),
    ).toBeVisible()
    await dialog.getByRole('button', { name: 'Закрыть', exact: true }).click()
    await page.reload()
    await page.getByRole('option', { name: /Council review/ }).click()
    await page
      .getByRole('region', { name: 'Планы этого диалога' })
      .getByRole('button', { name: /confirmed/ })
      .click()
    await page.getByRole('button', { name: 'Использовать план в Run…' }).click()
    dialog = page.getByRole('dialog', { name: 'Запустить задание' })
    await dialog
      .getByLabel('Привязка', { exact: true })
      .selectOption(binding.id)
    await expect(dialog.getByText(/Подтверждённый план Council:/)).toBeVisible()
    await dialog.getByRole('button', { name: 'Запустить preflight' }).click()
    await expect(
      dialog.getByRole('button', { name: 'Запустить', exact: true }),
    ).toBeEnabled()
    const runResponse = page.waitForResponse(
      (r) => r.url().endsWith('/api/runs') && r.request().method() === 'POST',
    )
    await dialog.getByRole('button', { name: 'Запустить', exact: true }).click()
    const runHttp = await runResponse
    expect(runHttp.status()).toBe(201)
    expect(runHttp.request().postDataJSON().planning_source).toMatchObject({
      job_id: job.id,
      revision_number: 3,
    })
    const run = await runHttp.json()
    const plan = await api(page, 'GET', `/runs/${run.id}/plan`)
    expect(plan.plan.original_text).toBe(edited.body_text)
    expect(plan.items[0].id).toBe('P1')
    expect(plan.items[0].acceptance_criteria).toEqual(
      edited.steps[0].acceptance_criteria,
    )
  } finally {
    server.closeAllConnections()
    await new Promise<void>((resolve) => server.close(() => resolve()))
  }
})

test('Council can be cancelled before the first revision', async ({ page }) => {
  await pair(page)
  const project = await api(page, 'POST', '/projects', {
    name: 'Cancel council',
    workspace_path: workspace(),
  })
  const chat = await api(page, 'POST', `/projects/${project.id}/chats`, {
    title: 'Cancel',
  })
  const provider = await api(page, 'POST', '/connections', {
    name: 'Uncalled',
    base_url: 'http://127.0.0.1:9/v1',
  })
  const job = await api(page, 'POST', '/planning_jobs', {
    project_id: project.id,
    chat_id: chat.id,
    task_text: 'Cancel before dispatch',
    idempotency_key: crypto.randomUUID(),
    participants: ['a', 'b', 'c'].map((model_id, i) => ({
      role: i === 2 ? 'merger' : 'participant',
      selection: {
        kind: 'direct',
        model_id,
        provider_connection_id: provider.id,
      },
    })),
  })
  await page.reload()
  await page.getByRole('option', { name: /Cancel council/ }).click()
  await page
    .getByRole('region', { name: 'Планы этого диалога' })
    .getByRole('button')
    .click()
  await page.getByRole('button', { name: 'Отменить подготовку' }).click()
  await expect(page.getByRole('dialog')).toContainText('cancelled')
  const final = await api(page, 'GET', `/planning_jobs/${job.id}`)
  expect(final.usage.external_calls).toBe(0)
  expect(final.revisions).toEqual([])
})

for (const unknown of [false, true]) {
  test(`Council retry: ${unknown ? 'explicit unknown consent' : 'rotated credentials and lost reply'}`, async ({
    page,
  }) => {
    test.setTimeout(60_000)
    page.setDefaultTimeout(10_000)
    const calls: { model: string; authorization: string | undefined }[] = []
    let phase = 'broken'
    const server = createServer(async (req, res) => {
      let raw = ''
      for await (const chunk of req) raw += chunk
      const body = JSON.parse(raw)
      calls.push({
        model: body.model,
        authorization: req.headers.authorization,
      })
      if (phase === 'broken' && body.model === 'x') {
        if (unknown) {
          req.socket.destroy()
          return
        }
        res.writeHead(401, { 'Content-Type': 'application/json' })
        res.end(JSON.stringify({ error: { message: 'Expired credential' } }))
        return
      }
      const content = document(
        body.model === 'merge'
          ? 'Merged plan after retry'
          : `Draft ${body.model}`,
      )
      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.end(
        JSON.stringify({
          choices: [
            {
              message: { role: 'assistant', content: JSON.stringify(content) },
            },
          ],
          usage: { total_tokens: 4 },
        }),
      )
    })
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
    try {
      await pair(page)
      const address = server.address() as { port: number }
      const project = await api(page, 'POST', '/projects', {
        name: `Retry council ${unknown}`,
        workspace_path: workspace(),
      })
      const chat = await api(page, 'POST', `/projects/${project.id}/chats`, {
        title: 'Retry',
      })
      const connection = await api(page, 'POST', '/connections', {
        name: `Retry provider ${unknown}`,
        base_url: `http://127.0.0.1:${address.port}/v1`,
        secret: 'synthetic-old-key',
      })
      const job = await api(page, 'POST', '/planning_jobs', {
        project_id: project.id,
        chat_id: chat.id,
        task_text: 'Retry after restoration',
        idempotency_key: crypto.randomUUID(),
        participants: ['x', 'y', 'merge'].map((model_id, i) => ({
          role: i === 2 ? 'merger' : 'participant',
          selection: {
            kind: 'direct',
            model_id,
            provider_connection_id: connection.id,
          },
        })),
      })
      const python = path.resolve(
        '../backend/.venv',
        process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
      )
      const dispatch = () =>
        promisify(execFile)(
          python,
          [
            path.resolve('tests/e2e/dispatch_council.py'),
            process.env.AGENTS_IDE_E2E_DATA_DIR!,
            job.id,
          ],
          { windowsHide: true, timeout: 20_000 },
        )
      await dispatch()
      const before = await api(page, 'GET', `/planning_jobs/${job.id}`)
      expect(before.state).toBe('failed')
      expect(before.last_error.code).toBe(
        unknown ? 'unknown_external_result' : 'council_quorum_missing',
      )
      expect(before.usage.external_calls).toBe(unknown ? 1 : 2)
      const accepted = before.drafts.filter(
        (d: { accepted: boolean }) => d.accepted,
      )
      await api(page, 'PATCH', `/connections/${connection.id}`, {
        expected_version: connection.version,
        secret: 'synthetic-new-key',
      })
      await page.reload()
      await page
        .getByRole('option', { name: `Retry council ${unknown}` })
        .click()
      await page
        .getByRole('region', { name: 'Планы этого диалога' })
        .getByRole('button')
        .click()
      const button = page.getByRole('button', {
        name: 'Повторить после восстановления доступа',
      })
      if (unknown) {
        await expect(button).toBeDisabled()
        await page
          .getByRole('checkbox', { name: /Предыдущий вызов мог выполниться/ })
          .check()
      } else {
        // Commit the mutation but lose its HTTP reply: refetch must leave failed UI.
        await page.route(
          `**/api/planning_jobs/${job.id}/retry`,
          async (route) => {
            const response = await route.fetch()
            expect(response.status()).toBe(200)
            await route.abort('failed')
          },
        )
      }
      phase = 'restored'
      const request = page.waitForRequest((r) =>
        r.url().endsWith(`/planning_jobs/${job.id}/retry`),
      )
      await button.click()
      expect((await request).postDataJSON().acknowledge_unknown_result).toBe(
        unknown,
      )
      await expect(page.getByRole('dialog')).toContainText('drafting')
      if (!unknown) await page.unroute(`**/api/planning_jobs/${job.id}/retry`)
      await dispatch()
      await expect(page.getByRole('dialog')).toContainText(
        'Merged plan after retry',
        { timeout: 15_000 },
      )
      const after = await api(page, 'GET', `/planning_jobs/${job.id}`)
      expect(after.state).toBe('ready_for_confirmation')
      expect(after.usage.external_calls).toBe(4)
      expect(after.started_at).toBe(before.started_at)
      expect(after.drafts).toEqual(expect.arrayContaining(accepted))
      expect(calls.map((c) => c.model)).toEqual(
        unknown ? ['x', 'x', 'y', 'merge'] : ['x', 'y', 'x', 'merge'],
      )
      expect(
        calls
          .slice(before.usage.external_calls)
          .every((c) => c.authorization === 'Bearer synthetic-new-key'),
      ).toBe(true)
      const events = after.events.filter(
        (e: { type: string }) => e.type === 'planning.retried',
      )
      expect(events).toHaveLength(1)
      expect(events[0].payload.acknowledged_unknown_members).toHaveLength(
        unknown ? 1 : 0,
      )
      expect(JSON.stringify(after)).not.toContain('synthetic-new-key')
    } finally {
      server.closeAllConnections()
      await new Promise<void>((resolve) => server.close(() => resolve()))
    }
  })
}

test('Council with a single accepted draft can be promoted to a degraded plan', async ({
  page,
}) => {
  test.setTimeout(60_000)
  page.setDefaultTimeout(10_000)
  const calls: { model: string }[] = []
  const server = createServer(async (req, res) => {
    let raw = ''
    for await (const chunk of req) raw += chunk
    const body = JSON.parse(raw)
    calls.push({ model: body.model })
    if (body.model === 'y') {
      res.writeHead(401, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ error: { message: 'Denied' } }))
      return
    }
    const content = document('Accepted draft', [questions[0]])
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end(
      JSON.stringify({
        choices: [
          {
            message: { role: 'assistant', content: JSON.stringify(content) },
          },
        ],
        usage: { total_tokens: 4 },
      }),
    )
  })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  try {
    await pair(page)
    const address = server.address() as { port: number }
    const project = await api(page, 'POST', '/projects', {
      name: 'Single-member promote',
      workspace_path: workspace(),
    })
    const chat = await api(page, 'POST', `/projects/${project.id}/chats`, {
      title: 'Promote',
    })
    const template = await api(page, 'POST', '/templates', {
      name: 'Promoted plan',
    })
    const version = await api(
      page,
      'POST',
      `/templates/${template.id}/versions`,
      {
        graph: {
          nodes: [
            { id: 'start', type: 'Start' },
            { id: 'end', type: 'End' },
          ],
          edges: [{ id: 'next', from: 'start', to: 'end' }],
        },
      },
    )
    const binding = await api(
      page,
      'POST',
      `/versions/${version.id}/bindings`,
      {
        project_id: project.id,
        name: 'Promoted plan',
      },
    )
    const connection = await api(page, 'POST', '/connections', {
      name: 'Single-member provider',
      base_url: `http://127.0.0.1:${address.port}/v1`,
      secret: 'synthetic-key',
    })
    const job = await api(page, 'POST', '/planning_jobs', {
      project_id: project.id,
      chat_id: chat.id,
      task_text: 'Single-member fallback',
      idempotency_key: crypto.randomUUID(),
      participants: ['x', 'y', 'merge'].map((model_id, i) => ({
        role: i === 2 ? 'merger' : 'participant',
        selection: {
          kind: 'direct',
          model_id,
          provider_connection_id: connection.id,
        },
      })),
    })
    const python = path.resolve(
      '../backend/.venv',
      process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
    )
    await promisify(execFile)(
      python,
      [
        path.resolve('tests/e2e/dispatch_council.py'),
        process.env.AGENTS_IDE_E2E_DATA_DIR!,
        job.id,
      ],
      { windowsHide: true, timeout: 20_000 },
    )
    const before = await api(page, 'GET', `/planning_jobs/${job.id}`)
    expect(before.state).toBe('failed')
    expect(before.last_error.code).toBe('council_quorum_missing')
    expect(before.n_participants_actual).toBe(1)
    expect(before.degraded).toBe(true)
    await page.reload()
    await page.getByRole('option', { name: 'Single-member promote' }).click()
    await page
      .getByRole('region', { name: 'Планы этого диалога' })
      .getByRole('button')
      .click()
    await expect(
      page.getByText('Кворум не набран: принят только один черновик.'),
    ).toBeVisible()
    const promoteButton = page.getByRole('button', {
      name: 'Принять единственный черновик',
    })
    await expect(promoteButton).toBeDisabled()
    await page
      .getByRole('checkbox', {
        name: /Подтверждаю, что это уменьшенный состав/,
      })
      .check()
    await expect(promoteButton).toBeEnabled()
    // The write succeeds but its reply is lost; the UI must recover by reading.
    await page.route(
      `**/api/planning_jobs/${job.id}/promote_single`,
      async (route) => {
        const response = await route.fetch()
        expect(response.status()).toBe(200)
        await route.abort('failed')
      },
    )
    await promoteButton.click()
    await expect(page.getByRole('dialog')).toContainText('needs_answers')
    await page.unroute(`**/api/planning_jobs/${job.id}/promote_single`)
    const promoted = await api(page, 'GET', `/planning_jobs/${job.id}`)
    expect(promoted.state).toBe('needs_answers')
    expect(promoted.last_error).toBeNull()
    expect(promoted.finished_at).toBeNull()
    expect(promoted.drafts).toEqual(before.drafts)
    expect(promoted.usage).toEqual(before.usage)
    expect(promoted.degraded).toBe(true)
    expect(promoted.n_participants_actual).toBe(1)
    expect(promoted.revisions).toHaveLength(1)
    expect(promoted.revisions[0].author).toBe('single_member')
    const event = promoted.events.find(
      (e: { type: string }) => e.type === 'planning.single_member_promoted',
    )
    expect(event?.payload.revision_number).toBe(1)
    // Failed drafts remain visible in the diagnostic list.
    expect(
      promoted.drafts.some((d: { accepted: boolean }) => !d.accepted),
    ).toBe(true)
    expect(calls.map((c) => c.model)).toEqual(['x', 'y'])
    expect(JSON.stringify(promoted)).not.toContain('synthetic-key')
    let dialog = page.getByRole('dialog')
    await expect(
      dialog.getByRole('button', { name: 'Подтвердить план' }),
    ).toHaveCount(0)
    await dialog
      .getByRole('combobox', { name: 'Database?', exact: true })
      .selectOption('a')
    await dialog
      .getByRole('button', { name: 'Сохранить ответы и правки' })
      .click()
    await expect(
      dialog.getByRole('heading', { name: /Ревизия 2/ }),
    ).toBeVisible()
    await dialog.getByRole('button', { name: 'Подтвердить план' }).click()
    await expect(dialog).toContainText('confirmed')
    await page.getByRole('button', { name: 'Использовать план в Run…' }).click()
    dialog = page.getByRole('dialog', { name: 'Запустить задание' })
    await dialog
      .getByLabel('Привязка', { exact: true })
      .selectOption(binding.id)
    await dialog.getByRole('button', { name: 'Запустить preflight' }).click()
    const start = dialog.getByRole('button', { name: 'Запустить', exact: true })
    await expect(start).toBeEnabled()
    const runResponse = page.waitForResponse(
      (r) => r.url().endsWith('/api/runs') && r.request().method() === 'POST',
    )
    await start.click()
    const run = await (await runResponse).json()
    await expect(
      page.getByText(
        /План Council принят в уменьшенном составе: участников 1\/2/,
      ),
    ).toBeVisible()
    const snapshot = await api(page, 'GET', `/runs/${run.id}/snapshot`)
    expect(snapshot.planning_provenance).toMatchObject({
      degraded: true,
      n_participants_actual: 1,
      n_participants_requested: 2,
      revision_author: 'user',
    })
    expect(calls).toHaveLength(2)
  } finally {
    server.closeAllConnections()
    await new Promise<void>((resolve) => server.close(() => resolve()))
  }
})
