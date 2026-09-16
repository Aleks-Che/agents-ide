import { execFileSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import path from 'node:path'
import type { Page } from '@playwright/test'
import { api, expect, pair, test, workspace } from './support'

function fixture(script: string, runId: string, mode?: string) {
  return execFileSync(
    path.resolve(
      '../backend/.venv',
      process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
    ),
    [
      `tests/e2e/${script}.py`,
      process.env.AGENTS_IDE_E2E_DATA_DIR!,
      runId,
      ...(mode ? [mode] : []),
    ],
    { encoding: 'utf8', windowsHide: true },
  ).trim()
}
async function createRun(page: Page) {
  const project = await api(page, 'POST', '/projects', {
    name: `Monitor ${randomUUID()}`,
    workspace_path: workspace(),
  })
  const connection = await api(page, 'POST', '/connections', {
    name: `local ${randomUUID()}`,
    base_url: 'http://127.0.0.1:9/v1',
  })
  const template = await api(page, 'POST', '/templates', {
    name: `Monitor ${randomUUID()}`,
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
            id: 'check',
            type: 'LLMRequest',
            config: {
              prompt: 'check',
              model_selection: {
                kind: 'direct',
                model_id: 'local-model',
                provider_connection_id: connection.id,
              },
            },
          },
          { id: 'end', type: 'End' },
        ],
        edges: [
          { id: 'first', from: 'start', to: 'check' },
          { id: 'last', from: 'check', to: 'end' },
        ],
      },
    },
  )
  const binding = await api(page, 'POST', `/versions/${version.id}/bindings`, {
    project_id: project.id,
    name: 'monitor',
  })
  const run = await api(page, 'POST', '/runs', {
    project_id: project.id,
    binding_id: binding.id,
    message: 'monitor',
    execution_mode: 'simulated',
    idempotency_key: randomUUID(),
    fake_scenario: {
      responses: [{ node_id: 'check', deltas: ['first', 'second'] }],
    },
  })
  return { project, run }
}
async function open(page: Page, projectName: string, runId: string) {
  await page.goto('/')
  await page.getByRole('option', { name: projectName }).click()
  await page.getByRole('button', { name: 'Запуски', exact: true }).click()
  await page
    .locator('li.profile-item')
    .filter({ has: page.getByText(runId.slice(0, 12), { exact: true }) })
    .getByRole('button', { name: 'Открыть', exact: true })
    .click()
  await expect(page.getByLabel('Граф выполнения')).toBeVisible()
}

test('virtual history, server filters, artifact chunks and disconnected API preserve Run state', async ({
  page,
}) => {
  await pair(page)
  const { project, run } = await createRun(page)
  fixture('seed_run_review', run.id, 'waiting')
  fixture('seed_observation', run.id)
  await open(page, project.name, run.id)
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByText('Поток: open', { exact: false })).toBeVisible()
  await expect(dialog.locator('.timeline-rows li')).toHaveCount(12)
  await dialog
    .getByRole('button', { name: 'Открыть сохранённую историю' })
    .click()
  await dialog.getByLabel('Фильтр событий').selectOption('messages')
  await expect(dialog.locator('.timeline-rows')).toContainText('message-649')
  await dialog.getByRole('button', { name: 'Старее', exact: true }).click()
  await expect(dialog.locator('.timeline-rows')).toContainText('message-449')
  await dialog.getByRole('button', { name: 'Новее', exact: true }).click()
  await expect(dialog.locator('.timeline-rows')).toContainText('message-649')
  await dialog.getByLabel('Лента событий').evaluate((element) => {
    element.scrollTop = 8000
  })
  await expect(dialog.locator('.timeline-rows li')).toHaveCount(12)
  await expect(dialog.locator('[data-untrusted]')).toHaveCount(0)
  await dialog.getByLabel('Фильтр событий').selectOption('tools')
  await expect(dialog.locator('.timeline-rows li')).toHaveCount(1)
  await dialog.locator('.timeline-rows button').click()
  await expect(
    dialog.getByRole('region', { name: 'Подробности события' }),
  ).toContainText('read')
  const diff = dialog.locator('.artifact-list > li').filter({ hasText: 'diff' })
  await diff.getByRole('button', { name: 'Открыть', exact: true }).click()
  await expect(diff.locator('pre')).toContainText('-old\n+new')
  await diff.getByRole('button', { name: 'Следующий фрагмент' }).click()
  await expect(diff).toContainText('Символы 16001')
  await dialog.getByRole('button', { name: 'Вернуться к потоку' }).click()
  // Abort every API connection, including the current SSE; the server state must not become stopped.
  await page.context().setOffline(true)
  await expect(
    dialog.getByText('Связь с API восстанавливается.', { exact: false }),
  ).toBeVisible({ timeout: 15000 })
  await expect(
    dialog.getByRole('heading', { name: /Run .*waiting_input/ }),
  ).toBeVisible()
  await page.context().setOffline(false)
  await expect(dialog.getByText('Поток: open', { exact: false })).toBeVisible({
    timeout: 15000,
  })
  expect(await api(page, 'GET', `/runs/${run.id}/commands`)).toHaveLength(0)
  await page.setViewportSize({ width: 390, height: 844 })
  expect(
    await dialog.evaluate(
      (element) => element.scrollWidth <= element.clientWidth,
    ),
  ).toBe(true)
  await page.screenshot({ path: '../.local/stage11-observation-mobile.png' })
})

test('two observers share controls; all pages close while the independent Runner finishes several steps', async ({
  page,
  context,
}) => {
  test.setTimeout(60000)
  await pair(page)
  const { project, run } = await createRun(page)
  await open(page, project.name, run.id)
  const second = await context.newPage()
  await open(second, project.name, run.id)
  expect(await api(page, 'GET', `/runs/${run.id}/commands`)).toHaveLength(0)
  await page.getByRole('button', { name: 'Пауза', exact: true }).click()
  // Queued control is accepted by the API and applied at the worker boundary.
  expect(fixture('execute_selection_run', run.id, 'execute')).toBe('paused')
  await expect(
    second.getByRole('heading', { name: /Run .*paused/ }),
  ).toBeVisible({ timeout: 10000 })
  await second.getByRole('button', { name: 'Продолжить', exact: true }).click()
  await expect(page.getByRole('heading', { name: /Run .*queued/ })).toBeVisible(
    { timeout: 10000 },
  )
  expect(await api(page, 'GET', `/runs/${run.id}/commands`)).toHaveLength(2)
  await second.close()
  await page.close()
  expect(context.pages()).toHaveLength(0)
  expect(fixture('execute_selection_run', run.id, 'execute')).toBe('completed')
  const reopened = await context.newPage()
  await open(reopened, project.name, run.id)
  await expect(
    reopened.getByRole('heading', { name: /Run .*completed/ }),
  ).toBeVisible()
  await expect(reopened.locator('.observed-edge-selected')).toHaveCount(1)
  await reopened.getByLabel('Подробности узла').selectOption('check')
  await expect(reopened.locator('.observed-detail')).toContainText(
    'local-model',
  )
  await expect(reopened.locator('.observed-detail')).toContainText('попыток 1')
  await reopened.getByLabel('Фильтр событий').selectOption('messages')
  await expect(reopened.locator('.timeline-rows li')).toHaveCount(2)
  const state = await api(reopened, 'GET', `/runs/${run.id}/snapshot`)
  expect(
    state.observation.nodes.every(
      (node: { status: string }) => node.status === 'succeeded',
    ),
  ).toBe(true)
  expect(await api(reopened, 'GET', `/runs/${run.id}/commands`)).toHaveLength(2)
  await reopened.setViewportSize({ width: 1440, height: 1000 })
  await reopened.screenshot({
    path: '../.local/stage11-observation-desktop.png',
  })
  await reopened.close()
})
