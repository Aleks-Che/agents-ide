import { randomUUID } from 'node:crypto'
import type { Page } from '@playwright/test'
import { test, expect, pair, api, workspace } from './support'

const uid = () => randomUUID().slice(0, 8)
async function setup(page: Page, imported = false) {
  await pair(page)
  const project = await api(page, 'POST', '/projects', {
    name: `Launch ${uid()}`,
    workspace_path: workspace(),
  })
  const chat = await api(page, 'POST', `/projects/${project.id}/chats`, {
    title: `Chat ${uid()}`,
  })
  const secondChat = await api(page, 'POST', `/projects/${project.id}/chats`, {
    title: `Other ${uid()}`,
  })
  const template = await api(page, 'POST', '/templates', {
    name: `Launch template ${uid()}`,
  })
  const version = await api(
    page,
    'POST',
    `/templates/${template.id}/versions`,
    {
      origin: imported ? 'imported' : 'local',
      graph: {
        nodes: [
          { id: 'start', type: 'Start' },
          { id: 'end', type: 'End' },
        ],
        edges: [{ id: 'edge', from: 'start', to: 'end' }],
        input_schema: {
          type: 'object',
          required: ['task'],
          properties: { task: { type: 'string', minLength: 1 } },
        },
      },
    },
  )
  const binding = await api(page, 'POST', `/versions/${version.id}/bindings`, {
    project_id: project.id,
    name: `Launch binding ${uid()}`,
  })
  await page.reload()
  await page.getByRole('option', { name: new RegExp(project.name) }).click()
  await page.getByRole('option', { name: new RegExp(chat.title) }).click()
  return { project, chat, secondChat, binding, version }
}

test('chat launch checks actual inputs and mode, retries a lost response after reload, and keeps chat histories separate', async ({
  page,
}) => {
  const { project, chat, secondChat, binding } = await setup(page)
  await api(page, 'POST', `/chats/${chat.id}/messages`, {
    role: 'user',
    content: 'saved context',
  })
  const other = await api(page, 'POST', '/runs', {
    project_id: project.id,
    chat_id: secondChat.id,
    binding_id: binding.id,
    inputs: { task: 'other' },
    execution_mode: 'simulated',
    idempotency_key: randomUUID(),
  })
  await page.getByLabel('Новое сообщение').fill('draft for this run')
  await page.getByRole('button', { name: 'Запустить шаблон' }).click()
  const dialog = page.getByRole('dialog')
  await expect(
    dialog.getByRole('button', { name: 'Запустить', exact: true }),
  ).toBeDisabled()
  await dialog
    .getByLabel('Шаблон проекта', { exact: true })
    .selectOption(binding.id)
  await dialog.getByLabel('Имитация (fake)').check()
  await dialog.getByRole('button', { name: 'Запустить preflight' }).click()
  await expect(
    dialog.getByRole('list', { name: 'Ошибки preflight' }),
  ).toContainText('task')
  await dialog
    .getByLabel('Входы запуска (JSON)')
    .fill('{"task":"review from UI"}')
  await dialog.getByLabel('Включить текущий черновик в запуск').check()
  await dialog.getByText('Лимиты и фильтр команд', { exact: true }).click()
  await dialog.getByLabel('Лимиты запуска (JSON)').fill('{"max_calls":19}')
  await dialog.getByRole('button', { name: 'Запустить preflight' }).click()
  await expect(
    dialog.getByRole('button', { name: 'Запустить', exact: true }),
  ).toBeEnabled()
  // Changing mode invalidates the report. The repeated check must use real.
  await dialog.getByLabel('Реальный', { exact: true }).check()
  await expect(
    dialog.getByRole('button', { name: 'Запустить', exact: true }),
  ).toBeDisabled()
  const checkedResponse = page.waitForResponse((response) =>
    response.url().endsWith(`/bindings/${binding.id}/preflight`),
  )
  await dialog.getByRole('button', { name: 'Запустить preflight' }).click()
  const checked = await (await checkedResponse).json()
  expect(checked.execution_mode).toBe('real')
  expect(checked.inputs).toEqual({ task: 'review from UI' })
  expect(checked.setting_sources['limit_overrides.max_calls']).toBe('run')
  // No worker is started: the real Start/End graph is only enqueued by this test.
  const requests: Record<string, unknown>[] = []
  await page.route('**/api/runs', async (route) => {
    if (route.request().method() !== 'POST') return route.continue()
    requests.push(route.request().postDataJSON())
    if (requests.length === 1) {
      const response = await route.fetch()
      expect(response.status()).toBe(201)
      await route.abort('failed')
    } else if (requests.length === 2) {
      // The retry can be rejected before the API checks the committed key.
      await route.fulfill({
        status: 403,
        contentType: 'application/json',
        body: JSON.stringify({
          code: 'csrf_invalid',
          message: 'test session renewed',
          details: {},
        }),
      })
    } else await route.continue()
  })
  await dialog.getByRole('button', { name: 'Запустить', exact: true }).click()
  await expect(
    dialog.getByRole('button', { name: 'Повторить тот же запуск' }),
  ).toBeEnabled()
  await dialog
    .getByRole('button', { name: 'Закрыть', exact: true })
    .last()
    .click()
  await expect(page.getByLabel('Новое сообщение')).toHaveValue(
    'draft for this run',
  )
  await page.reload()
  await page.getByRole('option', { name: new RegExp(project.name) }).click()
  await page.getByRole('option', { name: new RegExp(chat.title) }).click()
  await page.getByRole('button', { name: 'Запустить шаблон' }).click()
  await dialog.getByRole('button', { name: 'Повторить тот же запуск' }).click()
  await expect(dialog.getByRole('alert')).toContainText('test session renewed')
  await dialog.getByRole('button', { name: 'Повторить тот же запуск' }).click()
  await expect(dialog.getByRole('heading', { name: /Run ·/ })).toBeVisible()
  expect(requests).toHaveLength(3)
  expect(requests[0]).toEqual(requests[1])
  expect(requests[0]).toEqual(requests[2])
  expect(requests[0]).toMatchObject({
    trusted_execution_hash: checked.execution_hash,
    message: 'draft for this run',
    chat_id: chat.id,
  })
  const runs = await api(page, 'GET', `/runs?chat_id=${chat.id}`)
  expect(runs).toHaveLength(1)
  expect(runs[0].execution_hash).toBe(checked.execution_hash)
  await dialog.getByRole('button', { name: 'Закрыть экран Run' }).click()
  const history = page.getByRole('list', { name: 'Запуски диалога' })
  await expect(history).toContainText(runs[0].id.slice(0, 12))
  await expect(history).not.toContainText(other.id.slice(0, 12))
  await history.getByRole('button', { name: 'Открыть', exact: true }).click()
  await expect(dialog.getByRole('heading', { name: /Run ·/ })).toContainText(
    runs[0].id.slice(0, 12),
  )
  await page.keyboard.press('Escape')
  await page.getByRole('option', { name: new RegExp(secondChat.title) }).click()
  await expect(history).toContainText(other.id.slice(0, 12))
  await expect(history).not.toContainText(runs[0].id.slice(0, 12))
})

test('import consent is explicit and a binding changed after preflight cannot launch under a stale hash', async ({
  page,
}) => {
  const { project, chat, binding } = await setup(page, true)
  await page.getByRole('button', { name: 'Запустить шаблон' }).click()
  const dialog = page.getByRole('dialog')
  await dialog
    .getByLabel('Шаблон проекта', { exact: true })
    .selectOption(binding.id)
  await dialog.getByLabel('Входы запуска (JSON)').fill('{"task":"import test"}')
  await dialog.getByLabel('Имитация (fake)').check()
  await dialog.getByRole('button', { name: 'Запустить preflight' }).click()
  const consent = dialog.getByLabel('Доверяю импортированной конфигурации', {
    exact: false,
  })
  await expect(consent).toBeVisible()
  await expect(
    dialog.getByRole('button', { name: 'Запустить', exact: true }),
  ).toBeDisabled()
  await consent.check()
  await api(page, 'PATCH', `/bindings/${binding.id}`, {
    expected_version: binding.version,
    limit_overrides: { max_calls: 22 },
  })
  await dialog.getByRole('button', { name: 'Запустить', exact: true }).click()
  await expect(dialog.getByRole('alert')).toContainText('execution_hash')
  expect(await api(page, 'GET', `/runs?chat_id=${chat.id}`)).toHaveLength(0)
  await expect(dialog.getByLabel('Входы запуска (JSON)')).toHaveValue(
    '{"task":"import test"}',
  )
  await dialog.getByRole('button', { name: 'Запустить preflight' }).click()
  await expect(consent).not.toBeChecked()
  await consent.check()
  await page.setViewportSize({ width: 390, height: 700 })
  expect(
    await dialog.evaluate(
      (element) => element.scrollWidth <= element.clientWidth,
    ),
  ).toBe(true)
  await page.screenshot({ path: '../.local/stage9-chat-launch-review.png' })
  await dialog.getByRole('button', { name: 'Запустить', exact: true }).click()
  await expect(dialog.getByRole('heading', { name: /Run ·/ })).toBeVisible()
  const runs = await api(
    page,
    'GET',
    `/runs?project_id=${project.id}&chat_id=${chat.id}`,
  )
  expect(runs).toHaveLength(1)
})

test('launch displays group order and actual node overrides, and surfaces binding load failures', async ({
  page,
}) => {
  const { project, binding } = await setup(page)
  const connection = await api(page, 'POST', '/connections', {
    name: `Conn ${uid()}`,
    base_url: 'http://127.0.0.1:9/v1',
    manual_models: ['first', 'second', 'node-model'],
  })
  const group = await api(page, 'POST', '/model_groups/llm', {
    name: `Group ${uid()}`,
    members: [
      {
        provider_connection_id: connection.id,
        model_id: 'first',
        enabled: false,
      },
      { provider_connection_id: connection.id, model_id: 'second' },
    ],
  })
  const template = await api(page, 'POST', '/templates', {
    name: `Grouped ${uid()}`,
  })
  const version = await api(
    page,
    'POST',
    `/templates/${template.id}/versions`,
    {
      graph: {
        nodes: [
          { id: 'start', type: 'Start' },
          {
            id: 'review',
            type: 'LLMRequest',
            config: { role: 'verifier', prompt: 'review' },
          },
          {
            id: 'override',
            type: 'LLMRequest',
            config: {
              role: 'verifier',
              prompt: 'override',
              model_selection: {
                kind: 'direct',
                provider_connection_id: connection.id,
                model_id: 'node-model',
              },
            },
          },
          { id: 'end', type: 'End' },
        ],
        edges: [
          { id: 'e1', from: 'start', to: 'review' },
          { id: 'e2', from: 'review', to: 'override' },
          { id: 'e3', from: 'override', to: 'end' },
        ],
      },
    },
  )
  const grouped = await api(page, 'POST', `/versions/${version.id}/bindings`, {
    project_id: project.id,
    name: 'Grouped launch',
    model_selections: { verifier: { kind: 'group', group_id: group.id } },
  })
  await api(
    page,
    'POST',
    `/bindings/${binding.id}/archive?expected_version=${binding.version}`,
  )
  await page.route('**/api/bindings?*', (route) =>
    route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ message: 'test offline' }),
    }),
  )
  await page.reload()
  await page.getByRole('option', { name: new RegExp(project.name) }).click()
  await page.getByRole('option', { name: /Chat / }).click()
  await page.getByRole('button', { name: 'Запустить шаблон' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('alert')).toContainText('test offline')
  await page.unroute('**/api/bindings?*')
  await dialog
    .getByRole('button', { name: 'Повторить загрузку привязок' })
    .click()
  await dialog
    .getByLabel('Шаблон проекта', { exact: true })
    .selectOption(grouped.id)
  await expect(dialog.getByRole('option', { name: binding.name })).toHaveCount(
    0,
  )
  await dialog.getByLabel('Имитация (fake)').check()
  await dialog.getByRole('button', { name: 'Запустить preflight' }).click()
  const preview = dialog.getByRole('region', { name: 'Будут использованы' })
  await expect(preview).toContainText(`группа ${group.id}`)
  await expect(preview).toContainText('first · позиция 1 · disabled')
  await expect(preview).toContainText('second · позиция 2 · configured')
  await expect(preview).toContainText('node-model · позиция 1 · configured')
  await expect(
    dialog.getByRole('button', { name: 'Запустить', exact: true }),
  ).toBeEnabled()
  await page.setViewportSize({ width: 390, height: 700 })
  expect(
    await dialog.evaluate(
      (element) => element.scrollWidth <= element.clientWidth,
    ),
  ).toBe(true)
})
