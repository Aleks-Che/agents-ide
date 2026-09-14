import { test, expect } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import path from 'node:path'

test('pair, inspect real API state, reload and revoke session', async ({
  page,
}) => {
  const python = path.resolve(
    '../backend/.venv',
    process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
  )
  const dataDir = path.resolve('../.local/e2e-data')
  const code = execFileSync(
    python,
    [
      '-m',
      'agents_ide',
      '--data-dir',
      dataDir,
      '--port',
      '18767',
      'auth',
      'pair-code',
      '--rotate',
    ],
    { encoding: 'utf-8' },
  ).trim()
  await page.goto('/')
  await expect(
    page.getByRole('heading', { name: 'Подключить этот браузер' }),
  ).toBeVisible()
  await page.getByLabel('Код подключения').fill(code)
  await page.getByRole('button', { name: 'Подключиться' }).click()
  await expect(
    page.getByRole('heading', { name: 'Состояние служб' }),
  ).toBeVisible()
  await expect(page.getByText('Не запущен', { exact: true })).toBeVisible()
  await page.reload()
  await expect(
    page.getByRole('heading', { name: 'Состояние служб' }),
  ).toBeVisible()
  await page.getByRole('button', { name: 'Выйти', exact: true }).click()
  await expect(
    page.getByRole('heading', { name: 'Подключить этот браузер' }),
  ).toBeVisible()
  const unauthorized = await page.request.get('/api/system/status')
  expect(unauthorized.status()).toBe(401)
})
