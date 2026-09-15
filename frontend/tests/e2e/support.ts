import { test as base, expect, type Page } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { mkdirSync } from 'node:fs'
import { randomUUID } from 'node:crypto'
import path from 'node:path'

export const test = base.extend<{ pageErrors: string[] }>({
  pageErrors: [
    async ({ page }, use) => {
      const errors: string[] = []
      page.on('pageerror', (error) => errors.push(error.message))
      await use(errors)
      expect(errors).toEqual([])
    },
    { auto: true },
  ],
})
export { expect }

export async function pair(page: Page) {
  const python = path.resolve(
    '../backend/.venv',
    process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
  )
  const dataDir = process.env.AGENTS_IDE_E2E_DATA_DIR!
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
    { encoding: 'utf-8', windowsHide: true },
  ).trim()
  await page.goto('/')
  await page.getByLabel('Код подключения').fill(code)
  await page.getByRole('button', { name: 'Подключиться' }).click()
  await expect(
    page.getByRole('heading', { name: 'Состояние служб' }),
  ).toBeVisible()
}

export function workspace(): string {
  const directory = path.resolve(
    `${process.env.AGENTS_IDE_E2E_DATA_DIR}-workspaces`,
    randomUUID(),
    'Проект с пробелами',
  )
  mkdirSync(directory, { recursive: true })
  return directory
}

export async function api(
  page: Page,
  method: 'POST' | 'PATCH' | 'PUT' | 'GET',
  url: string,
  data?: unknown,
) {
  const session = await (await page.request.get('/api/auth/session')).json()
  const response = await page.request.fetch(`/api${url}`, {
    method,
    data,
    headers: {
      'X-CSRF-Token': session.csrf_token,
      Origin: 'http://127.0.0.1:18767',
    },
  })
  expect(response.ok(), await response.text()).toBeTruthy()
  return response.json()
}
