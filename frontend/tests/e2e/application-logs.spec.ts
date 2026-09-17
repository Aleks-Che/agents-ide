import { appendFileSync } from 'node:fs'
import path from 'node:path'
import { expect, pair, test } from './support'

test('saved errors can be filtered, downloaded and kept on screen when the server is unavailable', async ({
  page,
}) => {
  await pair(page)
  appendFileSync(
    path.join(process.env.AGENTS_IDE_E2E_DATA_DIR!, 'logs/worker.jsonl'),
    `${JSON.stringify({ at: '2000-01-01T00:00:00Z', level: 'ERROR', message: 'startup_failed' })}\n`,
  )
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  await page
    .getByRole('navigation', { name: 'Разделы настроек' })
    .getByRole('button', { name: 'Журнал', exact: true })
    .click()
  const panel = page.getByRole('region', { name: 'Журнал приложения' })
  await expect(
    panel.getByRole('combobox', { name: 'Период', exact: true }),
  ).toHaveValue('current')
  await expect(
    panel.getByText('В журнале текущего запуска ошибок и предупреждений нет.'),
  ).toBeVisible()
  await expect(panel.getByText('startup_failed', { exact: true })).toHaveCount(
    0,
  )
  await panel
    .getByRole('combobox', { name: 'Период', exact: true })
    .selectOption('all')
  await expect(panel.getByText('startup_failed', { exact: true })).toBeVisible()
  await expect(
    panel.getByText('Прошлый запуск', { exact: false }),
  ).toBeVisible()
  await panel
    .getByRole('combobox', { name: 'Период', exact: true })
    .selectOption('current')
  appendFileSync(
    path.join(process.env.AGENTS_IDE_E2E_DATA_DIR!, 'logs/worker.jsonl'),
    `${JSON.stringify({
      at: new Date().toISOString(),
      level: 'ERROR',
      message: 'database.schema_mismatch',
      expected_schema: 'new-schema',
      actual_schema: ['old-schema'],
    })}\n`,
  )
  await panel.getByRole('button', { name: 'Обновить журнал' }).click()
  await expect(
    panel.getByText('Версия базы данных не совпадает с версией приложения.'),
  ).toBeVisible()
  await panel
    .getByRole('combobox', { name: 'Служба', exact: true })
    .selectOption('worker')
  await panel.getByText('Подробности', { exact: true }).click()
  await expect(panel.locator('pre')).toContainText('new-schema')
  await expect(panel.locator('pre')).toContainText('old-schema')
  const downloadPromise = page.waitForEvent('download')
  await panel.getByRole('button', { name: 'Скачать журнал' }).click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe('agents-ide-logs.json')
  await panel
    .getByRole('combobox', { name: 'Служба', exact: true })
    .selectOption('start')
  await expect(
    panel.getByText('В текущем запуске записей выбранного уровня нет.'),
  ).toBeVisible()
  await panel
    .getByRole('combobox', { name: 'Служба', exact: true })
    .selectOption('worker')
  await expect(
    panel.getByRole('button', { name: 'Обновить журнал' }),
  ).toBeEnabled()
  await page.route('**/api/system/logs?**', (route) => route.abort())
  await panel.getByRole('button', { name: 'Обновить журнал' }).click()
  await expect(panel.getByRole('alert')).toContainText(
    'Не удалось обновить журнал',
  )
  await expect(
    panel.getByText('Версия базы данных не совпадает с версией приложения.'),
  ).toBeVisible()
  await expect(
    panel.getByText('./scripts/agents-ide.ps1 logs', { exact: true }),
  ).toBeVisible()
})
