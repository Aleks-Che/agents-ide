import { randomUUID } from 'node:crypto'
import { type Page } from '@playwright/test'
import { test, expect, api, pair } from './support'

test('native parameter choices follow model metadata and preserve an unsupported draft visibly', async ({
  page,
}) => {
  await pair(page)
  const suffix = randomUUID()
  const profile = await api(page, 'POST', '/harness_profiles', {
    name: `Native effort ${suffix}`,
    harness_kind: 'codex',
    settings: { permission_mode: 'read_only' },
  })
  const group = await api(page, 'POST', '/model_groups/agent', {
    name: `Native metadata ${suffix}`,
    members: [{ harness_profile_id: profile.id, model_id: 'alpha' }],
  })
  await page.route('**/api/harness_profiles*', async (route) => {
    const response = await route.fetch()
    const body = await response.json()
    await route.fulfill({
      response,
      json: Array.isArray(body)
        ? body.map((item) =>
            item.id === profile.id
              ? {
                  ...item,
                  model_capabilities: {
                    alpha: {
                      source: 'native_catalog',
                      reasoning_efforts: ['low', 'high'],
                    },
                    beta: {
                      source: 'native_catalog',
                      reasoning_efforts: ['low'],
                    },
                  },
                }
              : item,
          )
        : body,
    })
  })
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  const panel = page.getByRole('region', { name: 'Группы моделей' })
  await panel.getByRole('tab', { name: 'agent', exact: true }).click()
  await panel
    .getByRole('listitem')
    .filter({ hasText: group.name })
    .getByRole('button', { name: 'Параметры…' })
    .click()
  const dialog = page.getByRole('dialog')
  const member = dialog.locator('.member-edit-list > li').first()
  await member.getByRole('button', { name: /^Параметры/ }).click()
  await expect(
    member.getByLabel('Новый параметр').locator('option'),
  ).toHaveCount(1)
  await member.getByRole('button', { name: 'Добавить параметр' }).click()
  const effort = member.getByLabel('Значение reasoning_effort')
  await expect(effort.locator('option')).toHaveText(['low', 'high'])
  await effort.selectOption('high')
  await dialog
    .getByRole('button', { name: 'Сохранить кандидатов', exact: true })
    .click()
  await expect(
    dialog.getByText('Кандидаты сохранены.', { exact: true }),
  ).toBeVisible()
  expect(
    (await api(page, 'GET', `/model_groups/${group.id}`)).members[0].params,
  ).toEqual({ reasoning_effort: 'high' })
  await member.getByRole('textbox', { name: 'ID модели' }).fill('beta')
  await expect(effort).toHaveValue('high')
  await expect(effort).toHaveAttribute('aria-invalid', 'true')
  await expect(member.getByRole('alert')).toContainText(
    'не поддерживается выбранной моделью',
  )
})

async function editGroup(page: Page, params: Record<string, unknown>) {
  await pair(page)
  const suffix = randomUUID()
  const connection = await api(page, 'POST', '/connections', {
    name: `Params source ${suffix}`,
    base_url: 'http://127.0.0.1:9/v1',
  })
  const group = await api(page, 'POST', '/model_groups/llm', {
    name: `Params ${suffix}`,
    members: [
      { provider_connection_id: connection.id, model_id: 'first', params },
      { provider_connection_id: connection.id, model_id: 'second' },
    ],
  })
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  const panel = page.getByRole('region', { name: 'Группы моделей' })
  await panel.getByRole('tab', { name: 'llm', exact: true }).click()
  const item = panel.getByRole('listitem').filter({ hasText: group.name })
  await item.getByRole('button', { name: 'Параметры…' }).click()
  const dialog = page.getByRole('dialog')
  const member = dialog.locator('.member-edit-list > li').filter({
    has: page
      .getByRole('textbox', { name: 'ID модели' })
      .and(page.locator('[value="first"]')),
  })
  await member.getByRole('button', { name: /^Параметры/ }).click()
  return { group, item, dialog, member }
}

test('all parameter controls and exact stop sequences survive save, reopen and removal', async ({
  page,
}) => {
  const stop = ['\n\n', 'END\r\n', '', '  ', '\t"\\', 'конец']
  const { group, item, dialog, member } = await editGroup(page, { stop })
  expect(
    JSON.parse(await member.getByLabel('Значение stop').inputValue()),
  ).toEqual(stop)
  const expected = {
    stop: [...stop, 'next\nline', ''],
    reasoning_effort: 'high',
    temperature: 0.25,
    top_p: 0.8,
    max_tokens: 2048,
    max_output_tokens: 1024,
    seed: -42,
    frequency_penalty: -0.5,
    presence_penalty: 0.5,
    stream: false,
    structured_output: false,
    timeout_seconds: 0.5,
  }
  await member.getByLabel('Значение stop').fill(JSON.stringify(expected.stop))
  for (const [name, value] of Object.entries(expected)) {
    if (name === 'stop') continue
    await member.getByLabel('Новый параметр').selectOption(name)
    await member.getByRole('button', { name: 'Добавить параметр' }).click()
    const control = member.getByLabel(`Значение ${name}`, { exact: true })
    if (typeof value === 'number') await control.fill(String(value))
    else await control.selectOption(String(value))
  }
  await dialog
    .getByRole('button', { name: 'Сохранить кандидатов', exact: true })
    .click()
  await expect(
    dialog.getByText('Кандидаты сохранены.', { exact: true }),
  ).toBeVisible()
  expect(
    (await api(page, 'GET', `/model_groups/${group.id}`)).members[0].params,
  ).toEqual(expected)
  await page.keyboard.press('Escape')
  await item.getByRole('button', { name: 'Параметры…' }).click()
  await member.getByRole('button', { name: /^Параметры/ }).click()
  expect(
    JSON.parse(await member.getByLabel('Значение stop').inputValue()),
  ).toEqual(expected.stop)
  await member.getByLabel('Значение stop').fill('[]')
  await dialog
    .getByRole('button', { name: 'Сохранить кандидатов', exact: true })
    .click()
  await expect(
    dialog.getByText('Кандидаты сохранены.', { exact: true }),
  ).toBeVisible()
  expect(
    (await api(page, 'GET', `/model_groups/${group.id}`)).members[0].params
      .stop,
  ).toEqual([])
  await member.getByRole('button', { name: 'Удалить параметр stop' }).click()
  await dialog
    .getByRole('button', { name: 'Сохранить кандидатов', exact: true })
    .click()
  await expect(
    dialog.getByText('Кандидаты сохранены.', { exact: true }),
  ).toBeVisible()
  expect(
    (await api(page, 'GET', `/model_groups/${group.id}`)).members[0].params,
  ).not.toHaveProperty('stop')
})

test('invalid drafts stay with their candidate and cannot be copied or silently rounded', async ({
  page,
}) => {
  const { group, dialog, member } = await editGroup(page, {
    seed: 7,
    temperature: 0.5,
  })
  const save = dialog.getByRole('button', {
    name: 'Сохранить кандидатов',
    exact: true,
  })
  await save.click()
  await expect(
    dialog.getByText('Кандидаты сохранены.', { exact: true }),
  ).toBeVisible()
  const seed = member.getByLabel('Значение seed', { exact: true })
  await seed.fill('9007199254740993')
  await expect(seed).toHaveValue('9007199254740993')
  await expect(seed).toHaveAttribute('aria-invalid', 'true')
  await expect(member.getByRole('alert')).toContainText('точность')
  await expect(save).toBeDisabled()
  await expect(
    dialog.getByRole('button', { name: 'Копировать', exact: true }),
  ).toBeDisabled()
  await expect(
    dialog.getByText('Кандидаты сохранены.', { exact: true }),
  ).toHaveCount(0)
  await member.getByRole('button', { name: 'Ниже', exact: true }).click()
  await member.getByRole('button', { name: /^Параметры/ }).click()
  await expect(save).toBeDisabled()
  await member.getByRole('button', { name: /^Параметры/ }).click()
  await expect(seed).toHaveValue('9007199254740993')
  await dialog
    .getByLabel('Описание', { exact: true })
    .fill('Preserve parameter draft')
  await dialog.getByRole('button', { name: 'Сохранить', exact: true }).click()
  await expect(dialog.getByText('Название и описание сохранены.')).toBeVisible()
  await expect(seed).toHaveValue('9007199254740993')
  await seed.fill('-')
  await expect(seed).toHaveValue('-')
  await expect(save).toBeDisabled()
  await seed.pressSequentially('42')
  await expect(seed).toHaveValue('-42')
  const temperature = member.getByLabel('Значение temperature', { exact: true })
  await temperature.fill('1e-')
  await expect(temperature).toHaveValue('1e-')
  await expect(save).toBeDisabled()
  await temperature.pressSequentially('2')
  await expect(save).toBeEnabled()
  await page.route(`**/api/model_groups/${group.id}/llm/members`, (route) =>
    route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({
        code: 'temporarily_unavailable',
        message: 'Retry parameters',
      }),
    }),
  )
  await save.click()
  await expect(dialog.getByRole('alert')).toContainText('Retry parameters')
  await expect(temperature).toHaveValue('1e-2')
  await page.unroute(`**/api/model_groups/${group.id}/llm/members`)
  await save.click()
  await expect(
    dialog.getByText('Кандидаты сохранены.', { exact: true }),
  ).toBeVisible()
  const saved = await api(page, 'GET', `/model_groups/${group.id}`)
  expect(saved.members[1].model_id).toBe('first')
  expect(saved.members[1].params).toEqual({ seed: -42, temperature: 0.01 })
  await page.setViewportSize({ width: 390, height: 844 })
  await member.getByLabel('Новый параметр').selectOption('reasoning_effort')
  await member.getByRole('button', { name: 'Добавить параметр' }).click()
  await member.getByLabel('Новый параметр').selectOption('stop')
  await member.getByRole('button', { name: 'Добавить параметр' }).click()
  await member.getByLabel('Значение stop').scrollIntoViewIfNeeded()
  expect(
    (await member.getByLabel('LLM-подключение').boundingBox())!.width,
  ).toBeGreaterThan(150)
  const dimensions = await dialog.evaluate((element) => ({
    width: element.clientWidth,
    scroll: element.scrollWidth,
  }))
  expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.width + 1)
  await page.screenshot({ path: '../.local/stage9-member-params-review.png' })
})
