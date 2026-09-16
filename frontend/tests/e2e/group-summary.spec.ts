import { execFileSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import path from 'node:path'
import { api, expect, pair, test, workspace } from './support'

function execute(runId: string, action = 'execute') {
  return execFileSync(
    path.resolve(
      '../backend/.venv',
      process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
    ),
    [
      'tests/e2e/execute_selection_run.py',
      process.env.AGENTS_IDE_E2E_DATA_DIR!,
      runId,
      action,
    ],
    { encoding: 'utf-8', windowsHide: true },
  ).trim()
}

test('group diagnostics, safe event text, resume and durable executor after reopening', async ({
  page,
}) => {
  test.setTimeout(60000)
  await pair(page)
  const project = await api(page, 'POST', '/projects', {
    name: `Selection ${randomUUID()}`,
    workspace_path: workspace(),
  })
  await page.reload()
  await page.getByRole('option', { name: new RegExp(project.name) }).click()
  const connection = await api(page, 'POST', '/connections', {
    name: 'selection',
    base_url: 'http://127.0.0.1:9/v1',
  })
  const markup = '<b data-untrusted="yes">model</b>'
  const group = await api(page, 'POST', '/model_groups/llm', {
    name: `heavy ${randomUUID()}`,
    members: [
      {
        provider_connection_id: connection.id,
        model_id: 'disabled',
        enabled: false,
      },
      { provider_connection_id: connection.id, model_id: markup },
      { provider_connection_id: connection.id, model_id: 'backup' },
    ],
  })
  const template = await api(page, 'POST', '/templates', {
    name: 'Selection review',
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
            id: 'check',
            type: 'LLMRequest',
            max_retries: 0,
            config: {
              prompt: 'review',
              model_selection: { kind: 'group', group_id: group.id },
            },
          },
          { id: 'e', type: 'End' },
        ],
        edges: [
          { from: 's', to: 'check' },
          { from: 'check', to: 'e' },
        ],
      },
    },
  )
  const binding = await api(page, 'POST', `/versions/${version.id}/bindings`, {
    name: 'selection',
    project_id: project.id,
  })
  const run = await api(page, 'POST', '/runs', {
    project_id: project.id,
    binding_id: binding.id,
    execution_mode: 'simulated',
    message: 'go',
    idempotency_key: randomUUID(),
    fake_scenario: {
      responses: [1, 2].map((index) => ({
        node_id: 'check',
        attempt_index: index,
        outcome: 'unavailable',
        error_code: `quota_${index}`,
        retry_safety: 'safe',
        no_effect: true,
      })),
    },
  })
  expect(execute(run.id)).toBe('waiting_input')
  // A failed snapshot must be visible and retryable, rather than silently hiding the section.
  let fail = true
  await page.route(`**/api/runs/${run.id}/snapshot`, async (route) => {
    if (fail)
      await route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ code: 'offline', message: 'offline' }),
      })
    else await route.continue()
  })
  await page.getByRole('button', { name: 'Запуски', exact: true }).click()
  const open = async () =>
    page
      .locator('li.profile-item')
      .filter({ has: page.getByText(run.id.slice(0, 12), { exact: true }) })
      .getByRole('button', { name: 'Открыть', exact: true })
      .click()
  await open()
  const dialog = page.getByRole('dialog')
  const error = dialog
    .getByRole('alert')
    .filter({ hasText: 'Группа и кандидаты' })
  await expect(error).toBeVisible({ timeout: 15000 })
  await error.getByRole('button', { name: 'Повторить загрузку' }).click()
  fail = false
  const summary = dialog.getByRole('region', {
    name: 'Группа и кандидаты',
    exact: true,
  })
  await expect(summary).toContainText('Диагностика исчерпания', {
    timeout: 10000,
  })
  const node = summary.getByRole('region', { name: 'Кандидаты узла check' })
  await expect(node.locator('tbody tr')).toHaveCount(3)
  await expect(node.locator('tbody tr').nth(0)).toContainText('Пропущен')
  await expect(node.locator('tbody tr').nth(1)).toContainText('quota_1')
  await expect(node.locator('tbody tr').nth(2)).toContainText('quota_2')
  await expect(node.locator('[data-state="current"]')).toHaveCount(0)
  await dialog.getByLabel('Фильтр событий').selectOption('models')
  await expect(
    dialog
      .locator('.timeline-rows')
      .getByText(markup, { exact: false })
      .first(),
  ).toBeVisible({ timeout: 10000 })
  await expect(dialog.locator('[data-untrusted]')).toHaveCount(0)
  await expect(summary).toContainText(
    'Изменения группы применятся только к новым Run',
  )
  await dialog.getByRole('button', { name: 'Продолжить', exact: true }).click()
  await expect(
    dialog.getByRole('heading', { name: /Run · .* · queued/ }),
  ).toBeVisible()
  expect(execute(run.id)).toBe('completed')
  await expect(node.locator('.actual-selection')).toContainText(markup, {
    timeout: 10000,
  })
  await expect(node.locator('.actual-selection')).toContainText('succeeded')
  await expect(node).toContainText('раунд после исчерпания 1')
  expect((await api(page, 'GET', `/runs/${run.id}`)).snapshot_hash).toBe(
    run.snapshot_hash,
  )
  // Durable summary works even after all selection events have been retained away.
  execute(run.id, 'prune')
  await page.reload()
  await page.getByRole('option', { name: new RegExp(project.name) }).click()
  await page.getByRole('button', { name: 'Запуски', exact: true }).click()
  await open()
  await expect(node.locator('.actual-selection')).toContainText(markup)
  await expect(node.locator('tbody tr').nth(1)).toContainText('Выполнен')
  await node.getByText('История выбора узла check', { exact: false }).click()
  await expect(node.locator('.candidate-history')).toContainText('quota_1')
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(dialog).toBeVisible()
  const bounds = await dialog.boundingBox()
  expect(bounds!.width).toBeLessThanOrEqual(390)
  await page.screenshot({ path: '../.local/stage9-group-summary-review.png' })
})
