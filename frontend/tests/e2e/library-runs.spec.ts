import { execFileSync } from 'node:child_process'
import path from 'node:path'
import { randomUUID } from 'node:crypto'
import { test, expect, pair, api, workspace } from './support'
import type { Page } from '@playwright/test'

const uid = () => randomUUID().slice(0, 8)
async function project(page: Page) {
  const record = await api(page, 'POST', '/projects', {
    name: `Library ${uid()}`,
    workspace_path: workspace(),
  })
  await page.reload()
  await page.getByRole('option', { name: new RegExp(record.name) }).click()
  return record
}
async function roleBinding(page: Page, projectId: string, selections = {}) {
  const template = await api(page, 'POST', '/templates', {
    name: `Template ${uid()}`,
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
          { id: 'end', type: 'End' },
        ],
        edges: [
          { id: 'e1', from: 'start', to: 'review' },
          { id: 'e2', from: 'review', to: 'end' },
        ],
      },
    },
  )
  const binding = await api(page, 'POST', `/versions/${version.id}/bindings`, {
    project_id: projectId,
    name: `Binding ${uid()}`,
    model_selections: selections,
    role_parameters: { verifier: { temperature: 0.3 } },
  })
  return { template, version, binding }
}

test('library copies a preset and creates a project binding from its immutable version', async ({
  page,
}) => {
  await pair(page)
  const p = await project(page)
  await page.getByRole('button', { name: 'Шаблоны', exact: true }).click()
  await page
    .locator('.profile-item')
    .filter({ has: page.getByText('встроенный', { exact: true }) })
    .first()
    .click({ button: 'right' })
  await page.getByRole('menuitem', { name: 'Копировать', exact: true }).click()
  const dialog = page.getByRole('dialog')
  const name = `Copy ${uid()}`
  await dialog.getByLabel('Название шаблона').fill(name)
  await dialog.getByRole('button', { name: 'Копировать', exact: true }).click()
  await expect(dialog).toHaveCount(0)
  await page
    .locator('li.profile-item')
    .filter({ has: page.getByText(name, { exact: true }) })
    .click({ button: 'right' })
  await page
    .getByRole('menuitem', { name: 'Создать привязку', exact: true })
    .click()
  await dialog.getByLabel('Название в проекте').fill(`${name} binding`)
  await dialog
    .getByRole('button', { name: 'Создать привязку', exact: true })
    .click()
  await expect(dialog).toHaveCount(0)
  await page
    .getByRole('list', { name: 'Привязки проекта' })
    .getByRole('listitem')
    .first()
    .click({ button: 'right' })
  await page.getByRole('menuitem', { name: 'Параметры…' }).click()
  await expect(dialog.getByLabel('Название', { exact: true })).toHaveValue(
    `${name} binding`,
  )
  const bindings = await api(page, 'GET', `/bindings?project_id=${p.id}`)
  expect(bindings[0].limit_overrides).toEqual({})
  await dialog.getByLabel('Грязный каталог').selectOption('allow_nonoverlap')
  await dialog.getByRole('button', { name: 'Сохранить', exact: true }).click()
  await expect(dialog).toHaveCount(0)
  const updated = await api(page, 'GET', `/bindings/${bindings[0].id}`)
  expect(updated.dirty_policy).toBe('allow_nonoverlap')
  expect(updated.limit_overrides).toEqual({})
  expect(updated.project_id).toBe(p.id)

  const list = page.getByRole('list', { name: 'Привязки проекта' })
  const card = list
    .getByRole('listitem')
    .filter({ has: page.getByText(updated.name, { exact: true }) })
  await expect(card.getByRole('button')).toHaveCount(0)
  await card.focus()
  await card.press('Shift+F10')
  const menu = page.getByRole('menu', {
    name: `Действия с привязкой «${updated.name}»`,
  })
  await expect(menu.getByRole('menuitem')).toHaveText(['Параметры…', 'Удалить'])
  await page.keyboard.press('Escape')
  await expect(menu).toHaveCount(0)
  // A stale revision must keep the card; the refreshed revision allows retry.
  await api(page, 'PATCH', `/bindings/${updated.id}`, {
    expected_version: updated.version,
    dirty_policy: 'strict',
  })
  await card.click({ button: 'right' })
  await menu.getByRole('menuitem', { name: 'Удалить', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText(
    'Привязка изменена в другом месте',
  )
  await expect(card).toBeVisible()
  await card.click({ button: 'right' })
  await menu.getByRole('menuitem', { name: 'Удалить', exact: true }).click()
  await expect(card).toHaveCount(0)
  expect((await api(page, 'GET', `/bindings/${updated.id}`)).archived).toBe(
    true,
  )
  const definition = await api(page, 'GET', `/versions/${updated.version_id}`)
  expect(
    (await api(page, 'GET', `/templates/${definition.template_id}`)).archived,
  ).toBe(false)
  await page.reload()
  await page.getByRole('option', { name: new RegExp(p.name) }).click()
  await page.getByRole('button', { name: 'Шаблоны', exact: true }).click()
  await expect(
    page.getByText('Шаблоны ещё не привязаны.', { exact: false }),
  ).toBeVisible()
})

test('binding editor preserves drafts on conflict and repairs an archived group', async ({
  page,
}) => {
  await pair(page)
  const p = await project(page)
  const connection = await api(page, 'POST', '/connections', {
    name: `Conn ${uid()}`,
    base_url: 'http://127.0.0.1:9/v1',
    manual_models: ['m'],
  })
  const group = await api(page, 'POST', '/model_groups/llm', {
    name: `Group ${uid()}`,
    members: [{ provider_connection_id: connection.id, model_id: 'm' }],
  })
  const { binding } = await roleBinding(page, p.id, {
    verifier: { kind: 'group', group_id: group.id },
  })
  await api(
    page,
    'POST',
    `/model_groups/${group.id}/archive?expected_revision=${group.revision}`,
  )
  await page.getByRole('button', { name: 'Шаблоны', exact: true }).click()
  await page
    .getByRole('list', { name: 'Привязки проекта' })
    .getByRole('listitem')
    .first()
    .click({ button: 'right' })
  await page.getByRole('menuitem', { name: 'Параметры…' }).click()
  const dialog = page.getByRole('dialog')
  await expect(
    dialog.getByText('Группа недоступна', { exact: false }),
  ).toBeVisible()
  await dialog.getByRole('tab', { name: 'Прямая модель' }).click()
  await dialog.getByLabel('ID модели', { exact: true }).fill('m')
  await dialog.getByRole('button', { name: 'Сохранить', exact: true }).click()
  await expect(
    dialog.getByRole('alert').filter({ hasText: 'Выберите LLM' }),
  ).toBeVisible()
  await dialog.getByLabel('LLM-подключение').selectOption(connection.id)
  await dialog.getByRole('button', { name: 'Сохранить', exact: true }).click()
  await expect(dialog).toHaveCount(0)
  let saved = await api(page, 'GET', `/bindings/${binding.id}`)
  expect(saved.role_parameters.verifier.temperature).toBe(0.3)
  expect(saved.model_selections.verifier.kind).toBe('direct')
  await page
    .getByRole('list', { name: 'Привязки проекта' })
    .getByRole('listitem')
    .first()
    .click({ button: 'right' })
  await page.getByRole('menuitem', { name: 'Параметры…' }).click()
  await dialog.getByLabel('Название', { exact: true }).fill('local draft')
  await api(page, 'PATCH', `/bindings/${binding.id}`, {
    expected_version: saved.version,
    name: 'external change',
  })
  await dialog.getByRole('button', { name: 'Сохранить', exact: true }).click()
  await expect(dialog.getByLabel('Название', { exact: true })).toHaveValue(
    'local draft',
  )
  await dialog
    .getByRole('button', { name: 'Загрузить текущую версию и сбросить правки' })
    .click()
  await expect(dialog.getByLabel('Название', { exact: true })).toHaveValue(
    'external change',
  )
  // A temporary session-check failure must not unmount the editor or discard its draft.
  await dialog.getByLabel('Название', { exact: true }).fill('offline draft')
  await page.route('**/api/auth/session', (route) => route.abort('failed'))
  const failedSession = page.waitForEvent('requestfailed', {
    predicate: (request) => request.url().endsWith('/api/auth/session'),
  })
  await page.evaluate(() => window.dispatchEvent(new Event('visibilitychange')))
  await failedSession
  await expect(dialog.getByLabel('Название', { exact: true })).toHaveValue(
    'offline draft',
  )
  await page.unroute('**/api/auth/session')
  await dialog
    .getByRole('button', { name: 'Проверить сохранённую привязку' })
    .click()
  await expect(
    dialog
      .getByRole('region', { name: 'Проверка готовности' })
      .getByRole('status'),
  ).toBeVisible()
  saved = await api(page, 'GET', `/bindings/${binding.id}`)
  expect(saved.model_selections.verifier.model_id).toBe('m')
  await page.setViewportSize({ width: 390, height: 700 })
  const bounds = await dialog.boundingBox()
  expect(bounds!.width).toBeLessThanOrEqual(390)
  expect(
    await dialog.evaluate(
      (element) => element.scrollWidth <= element.clientWidth,
    ),
  ).toBe(true)
  await page.screenshot({ path: '../.local/stage9-binding-review.png' })
})

function checkpoint(runId: string, mode: 'completed' | 'waiting') {
  execFileSync(
    path.resolve(
      '../backend/.venv',
      process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
    ),
    [
      path.resolve('tests/e2e/seed_run_review.py'),
      process.env.AGENTS_IDE_E2E_DATA_DIR!,
      runId,
      mode,
    ],
    { windowsHide: true },
  )
}

test('Run screen selects the requested run, replays retained events, resolves and resumes with durable command IDs', async ({
  page,
}) => {
  await pair(page)
  const p = await project(page)
  const template = await api(page, 'POST', '/templates', {
    name: `Runs ${uid()}`,
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
        edges: [{ id: 'e', from: 'start', to: 'end' }],
      },
    },
  )
  const binding = await api(page, 'POST', `/versions/${version.id}/bindings`, {
    project_id: p.id,
    name: `Runs binding ${uid()}`,
  })
  const start = () =>
    api(page, 'POST', '/runs', {
      project_id: p.id,
      binding_id: binding.id,
      message: 'test',
      execution_mode: 'simulated',
      idempotency_key: randomUUID(),
    })
  const first = await start()
  checkpoint(first.id, 'completed')
  const second = await start()
  checkpoint(second.id, 'waiting')
  const originalHash = (await api(page, 'GET', `/runs/${second.id}`))
    .snapshot_hash
  const streamRequests: string[] = []
  page.on('request', (request) => {
    if (request.url().includes(`/runs/${second.id}/stream`))
      streamRequests.push(request.url())
  })
  await page.getByRole('button', { name: 'Запуски', exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await page
    .locator('li.profile-item')
    .filter({ has: page.getByText(first.id.slice(0, 12), { exact: true }) })
    .getByRole('button', { name: 'Открыть', exact: true })
    .click()
  const dialog = page.getByRole('dialog')
  await expect(
    dialog.getByRole('heading', { name: new RegExp(first.id.slice(0, 12)) }),
  ).toBeVisible()
  await dialog.getByRole('button', { name: 'Закрыть экран Run' }).click()
  await expect(dialog).toHaveCount(0)
  await page
    .locator('li.profile-item')
    .filter({ has: page.getByText(second.id.slice(0, 12), { exact: true }) })
    .getByRole('button', { name: 'Открыть', exact: true })
    .click()
  await expect(dialog.getByText('Поток: open', { exact: false })).toBeVisible()
  await expect(dialog.locator('.timeline-rows > li')).toHaveCount(12)
  await expect(
    dialog.getByText('Курсор недоступен:', { exact: false }),
  ).toBeVisible()
  expect(streamRequests).toHaveLength(1)
  const alpha = dialog
    .locator('.artifact-list > li')
    .filter({ hasText: 'review_alpha' })
  const beta = dialog
    .locator('.artifact-list > li')
    .filter({ hasText: 'review_beta' })
  await alpha.getByRole('button', { name: 'Открыть', exact: true }).click()
  await expect(alpha).toContainText('alpha-body')
  await expect(beta).not.toContainText('alpha-body')
  await dialog.getByRole('button', { name: 'Решить', exact: true }).click()
  await dialog.getByLabel('Новый лимит max_calls').fill('200')
  await dialog.getByRole('button', { name: 'Сохранить решение' }).click()
  await expect(
    dialog.getByRole('form', { name: 'Решение ожидания' }),
  ).toHaveCount(0)
  await expect
    .poll(
      async () =>
        (await api(page, 'GET', `/runs/${second.id}/commands`)).length,
    )
    .toBe(1)
  const resolved = await api(page, 'GET', `/runs/${second.id}`)
  expect(resolved.runtime.limit_overrides.max_calls).toBe(200)
  expect(resolved.snapshot_hash).toBe(originalHash)
  // Drop a successful response: retry must reuse the original command ID and version.
  let dropped = false
  const requests: unknown[] = []
  await page.route(`**/api/runs/${second.id}/commands`, async (route) => {
    if (route.request().method() !== 'POST') return route.continue()
    requests.push(route.request().postDataJSON())
    if (!dropped) {
      dropped = true
      await route.fetch()
      await route.abort('failed')
    } else await route.continue()
  })
  await dialog.getByRole('button', { name: 'Продолжить', exact: true }).click()
  await expect(
    dialog.getByRole('button', { name: 'Проверить отправленную команду' }),
  ).toBeVisible()
  await dialog
    .getByRole('button', { name: 'Проверить отправленную команду' })
    .click()
  await expect(
    dialog.getByRole('heading', { name: new RegExp('queued') }),
  ).toBeVisible()
  expect(requests).toHaveLength(2)
  expect(requests[0]).toEqual(requests[1])
  expect((await api(page, 'GET', `/runs/${second.id}/commands`)).length).toBe(2)
  expect(streamRequests).toHaveLength(1)
  await page.screenshot({ path: '../.local/stage9-run-review.png' })
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
})
