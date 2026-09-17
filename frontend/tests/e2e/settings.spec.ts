import { createServer } from 'node:http'
import { randomUUID } from 'node:crypto'
import { test, expect, pair, api } from './support'
import { installedHarness } from './harness-fixture'

test('agent group can be created, extended, disabled, removed and archived in UI', async ({
  page,
}) => {
  await pair(page)
  const suffix = randomUUID()
  const harness = await installedHarness(page)
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  await page
    .getByRole('navigation', { name: 'Разделы настроек' })
    .getByRole('button', { name: 'Группы моделей', exact: true })
    .click()
  const panel = page.getByRole('region', { name: 'Группы моделей' })
  await panel.getByRole('button', { name: '+ Новая' }).click()
  let dialog = page.getByRole('dialog')
  const name = `Agent group ${suffix}`
  await dialog.getByLabel('Название', { exact: true }).fill(name)
  await dialog.getByLabel('Harness', { exact: true }).selectOption(harness.id)
  await dialog.getByLabel('Модель harness').selectOption('test/first')
  await dialog.getByRole('button', { name: 'Создать', exact: true }).click()
  const item = panel
    .locator('.profile-item')
    .filter({ has: page.getByText(name, { exact: true }) })
  await expect(item).toContainText('OpenCode')
  await item.getByRole('button', { name: 'Параметры…' }).click()
  dialog = page.getByRole('dialog')
  await dialog.getByRole('button', { name: '+ Добавить' }).click()
  await dialog.getByLabel('Модель harness').nth(1).selectOption('test/second')
  await dialog.getByRole('checkbox').first().uncheck()
  await dialog.getByRole('button', { name: 'Сохранить', exact: true }).click()
  await expect(dialog.getByRole('status')).toContainText('Группа сохранена')
  await page.keyboard.press('Escape')
  await expect(item).toContainText('отключён')
  await item.getByRole('button', { name: 'Параметры…' }).click()
  await expect(dialog.getByLabel('Модель harness').nth(1)).toHaveValue(
    'test/second',
  )
  await dialog
    .getByRole('button', { name: 'Параметры', exact: true })
    .nth(1)
    .click()
  await dialog.getByLabel('Новый параметр').selectOption('temperature')
  await dialog.getByRole('button', { name: 'Добавить параметр' }).click()
  await dialog.getByLabel('Значение temperature').fill('0.5')
  await dialog.getByRole('button', { name: 'Сохранить', exact: true }).click()
  await expect(dialog.getByRole('status')).toContainText('Группа сохранена')
  let groups = await api(page, 'GET', '/model_groups?kind=agent')
  let saved = groups.find((group: { name: string }) => group.name === name)
  expect(
    saved.members.find(
      (member: { model_id: string }) => member.model_id === 'test/second',
    ).params,
  ).toEqual({ temperature: 0.5 })
  await expect(
    dialog.getByRole('button', { name: 'Параметры (1)', exact: true }),
  ).toBeVisible()
  await dialog.getByLabel('Значение temperature').fill('3')
  await expect(dialog.getByRole('alert').first()).toContainText(
    'Нужно число от 0 до 2',
  )
  await expect(
    dialog.getByRole('button', { name: 'Сохранить', exact: true }),
  ).toBeDisabled()
  await dialog.getByLabel('Значение temperature').fill('0.7')
  await dialog.getByRole('button', { name: 'Сохранить', exact: true }).click()
  await expect(dialog.getByRole('status')).toContainText('Группа сохранена')
  await dialog
    .getByRole('button', { name: 'Удалить параметр temperature' })
    .click()
  await dialog.getByRole('button', { name: 'Сохранить', exact: true }).click()
  await expect(dialog.getByRole('status')).toContainText('Группа сохранена')
  groups = await api(page, 'GET', '/model_groups?kind=agent')
  saved = groups.find((group: { name: string }) => group.name === name)
  expect(
    saved.members.find(
      (member: { model_id: string }) => member.model_id === 'test/second',
    ).params,
  ).toEqual({})
  await dialog
    .getByRole('button', { name: 'Удалить', exact: true })
    .first()
    .click()
  await dialog.getByRole('button', { name: 'Сохранить', exact: true }).click()
  await expect(dialog.getByRole('status')).toContainText('Группа сохранена')
  await dialog
    .getByRole('button', { name: 'Архивировать', exact: true })
    .click()
  await expect(item).toHaveCount(0)
})

test('connection key replacement keeps manual models; test, rename and archive use real API', async ({
  page,
}) => {
  const authorization: string[] = []
  const server = createServer((request, response) => {
    authorization.push(request.headers.authorization ?? '')
    response.setHeader('Content-Type', 'application/json')
    response.end(
      JSON.stringify(
        request.url?.endsWith('/models')
          ? { data: [{ id: 'e2e-model' }] }
          : {
              choices: [{ message: { content: 'OK' }, finish_reason: 'stop' }],
            },
      ),
    )
  })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  try {
    const address = server.address()
    if (!address || typeof address === 'string')
      throw new Error('Server did not bind')
    await pair(page)
    await page.getByRole('button', { name: 'Настройки', exact: true }).click()
    await page
      .getByRole('navigation', { name: 'Разделы настроек' })
      .getByRole('button', { name: 'LLM-подключения', exact: true })
      .click()
    const panel = page.getByRole('region', { name: 'LLM-подключения' })
    await panel.getByRole('button', { name: '+ Новое' }).click()
    const name = `Local ${randomUUID()}`
    let dialog = page.getByRole('dialog', { name: 'Новое LLM-подключение' })
    await dialog.getByLabel('Название').fill(name)
    await dialog
      .getByLabel('Base URL')
      .fill(`http://127.0.0.1:${address.port}/v1`)
    await dialog.getByLabel('Ключ', { exact: true }).fill('e2e-original')
    await dialog.getByLabel('Ручные модели').fill('original-model')
    await dialog.getByRole('button', { name: 'Создать' }).click()
    const item = panel.getByRole('listitem').filter({ hasText: name })
    await item.getByRole('button', { name: 'Параметры…' }).click()
    dialog = page.getByRole('dialog', { name, exact: true })
    await expect(dialog.getByLabel('Новый ключ')).toHaveValue('')
    await dialog.getByLabel('Новый ключ').fill('e2e-replaced')
    await expect(dialog.getByLabel('Новый ключ')).toHaveAttribute(
      'type',
      'password',
    )
    await dialog.getByLabel('Ручные модели').fill('replacement-model')
    await dialog.getByRole('button', { name: 'Сохранить', exact: true }).click()
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await item.getByRole('button', { name: 'Параметры…' }).click()
    await expect(dialog.getByLabel('Ручные модели')).toHaveValue(
      'replacement-model',
    )
    await dialog.getByRole('button', { name: 'Тест', exact: true }).click()
    await expect(dialog.getByRole('status')).toContainText('Тест · ok')
    await expect(
      dialog.getByRole('list', { name: 'Модели подключения' }),
    ).toContainText('e2e-model')
    expect(authorization.length).toBeGreaterThanOrEqual(2)
    expect(
      authorization.every((value) => value === 'Bearer e2e-replaced'),
    ).toBeTruthy()
    await dialog.getByLabel('Название').fill(`${name} edited`)
    await dialog.getByRole('button', { name: 'Сохранить', exact: true }).click()
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await item.getByRole('button', { name: 'Параметры…' }).click()
    dialog = page.getByRole('dialog')
    await expect(
      dialog.getByRole('list', { name: 'Модели подключения' }),
    ).toContainText('e2e-model')
    await dialog
      .getByRole('button', { name: 'Архивировать', exact: true })
      .click()
    await expect(item).toHaveCount(0)
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    )
  }
})

test('group save persists all drafts, handles conflict explicitly and shows copy errors', async ({
  page,
}) => {
  await pair(page)
  const suffix = randomUUID()
  const connection = await api(page, 'POST', '/connections', {
    name: `Group source ${suffix}`,
    base_url: 'http://127.0.0.1:9/v1',
  })
  const group = await api(page, 'POST', '/model_groups/llm', {
    name: `Group ${suffix}`,
    description: 'Old description',
    members: [
      { provider_connection_id: connection.id, model_id: 'first' },
      {
        provider_connection_id: connection.id,
        model_id: 'second',
        params: { temperature: 0.2 },
      },
    ],
  })
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  await page
    .getByRole('navigation', { name: 'Разделы настроек' })
    .getByRole('button', { name: 'Группы моделей', exact: true })
    .click()
  const panel = page.getByRole('region', { name: 'Группы моделей' })
  await panel.getByRole('tab', { name: 'llm', exact: true }).click()
  const item = panel
    .getByRole('listitem')
    .filter({ has: page.locator('strong', { hasText: group.name }) })
  await item.getByRole('button', { name: 'Параметры…' }).click()
  let dialog = page.getByRole('dialog')
  await dialog
    .getByLabel('Название', { exact: true })
    .fill(`${group.name} renamed`)
  await dialog.getByLabel('Описание').fill('')
  await dialog
    .getByRole('button', { name: 'Ниже', exact: true })
    .first()
    .click()
  await dialog.getByRole('button', { name: 'Сохранить', exact: true }).click()
  await expect(dialog.getByRole('status')).toContainText('Группа сохранена')
  await expect(dialog.getByLabel('Название', { exact: true })).toHaveValue(
    `${group.name} renamed`,
  )
  const saved = await api(page, 'GET', `/model_groups/${group.id}`)
  expect(saved.name).toBe(`${group.name} renamed`)
  expect(saved.description).toBe('')
  expect(
    saved.members.map((member: { model_id: string }) => member.model_id),
  ).toEqual(['second', 'first'])
  expect(saved.members[0].params).toEqual({ temperature: 0.2 })
  await api(page, 'PATCH', `/model_groups/${group.id}/llm`, {
    expected_revision: saved.revision,
    name: `${group.name} external`,
  })
  await dialog.getByLabel('Название', { exact: true }).fill('Local draft')
  await page.evaluate(() => window.dispatchEvent(new Event('focus')))
  await expect(dialog.getByLabel('Название', { exact: true })).toHaveValue(
    'Local draft',
  )
  await dialog.getByRole('button', { name: 'Сохранить', exact: true }).click()
  await expect(dialog.getByRole('alert')).toBeVisible()
  await expect(dialog.getByLabel('Название', { exact: true })).toHaveValue(
    'Local draft',
  )
  await dialog
    .getByRole('button', { name: 'Загрузить текущую версию и сбросить правки' })
    .click()
  await expect(dialog.getByLabel('Название', { exact: true })).toHaveValue(
    `${group.name} external`,
  )
  await dialog.getByRole('button', { name: 'Копировать', exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await panel
    .getByRole('listitem')
    .filter({ has: page.getByText(`${group.name} external`, { exact: true }) })
    .getByRole('button', { name: 'Параметры…' })
    .click()
  dialog = page.getByRole('dialog')
  await dialog.getByRole('button', { name: 'Копировать', exact: true }).click()
  await expect(dialog.getByRole('alert')).toBeVisible()
})

test('small viewport keeps navigation, modal controls and keyboard focus usable', async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 700 })
  await pair(page)
  await expect(page.getByRole('button', { name: 'Новый проект' })).toBeVisible()
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  await page
    .getByRole('navigation', { name: 'Разделы настроек' })
    .getByRole('button', { name: 'LLM-подключения', exact: true })
    .click()
  await page
    .getByRole('region', { name: 'LLM-подключения' })
    .getByRole('button', { name: '+ Новое' })
    .click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  for (let index = 0; index < 12; index++) {
    await page.keyboard.press('Tab')
    expect(
      await page.evaluate(
        () => document.activeElement?.closest('dialog') !== null,
      ),
    ).toBeTruthy()
  }
  const dimensions = await dialog.evaluate((element) => ({
    scroll: element.scrollWidth,
    width: element.clientWidth,
  }))
  expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.width + 1)
  const bounds = await dialog.boundingBox()
  expect(bounds!.y).toBeGreaterThanOrEqual(0)
  expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(700)
  await page.screenshot({ path: '../.local/stage9-settings-mobile.png' })
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
  await page.setViewportSize({ width: 1440, height: 960 })
  await page.screenshot({
    path: '../.local/stage9-settings-desktop.png',
    fullPage: true,
  })
})

test('installed harness settings select a native default without creating profiles', async ({
  page,
}) => {
  await pair(page)
  await installedHarness(page, 'codex')
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  await page
    .getByRole('navigation', { name: 'Разделы настроек' })
    .getByRole('button', { name: 'Агенты', exact: true })
    .click()
  const panel = page.getByRole('region', { name: 'Установленные harness' })
  await expect(panel.getByRole('button', { name: '+ Новый' })).toHaveCount(0)
  await panel.getByRole('button', { name: 'Codex', exact: true }).click()
  let dialog = page.getByRole('dialog')
  await expect(dialog.getByLabel('Исполняемый файл')).toHaveValue(
    'C:/tools/codex.exe',
  )
  await dialog.getByLabel('Модель по умолчанию').selectOption('test/second')
  await dialog.getByRole('button', { name: 'Сохранить', exact: true }).click()
  await expect(panel).toContainText('Модель по умолчанию · test/second')
  await panel.getByRole('button', { name: 'Обновить список' }).click()
  await expect(panel.getByRole('listitem')).toHaveCount(1)
  await panel.getByRole('button', { name: 'Codex', exact: true }).click()
  await expect(page.getByLabel('Модель по умолчанию')).toHaveValue(
    'test/second',
  )
  await page.keyboard.press('Escape')
  await page
    .getByRole('navigation', { name: 'Разделы настроек' })
    .getByRole('button', { name: 'Группы моделей', exact: true })
    .click()
  await page
    .getByRole('region', { name: 'Группы моделей' })
    .getByRole('button', { name: '+ Новая' })
    .click()
  dialog = page.getByRole('dialog')
  await dialog
    .getByLabel('Harness', { exact: true })
    .selectOption({ label: 'Codex' })
  await expect(dialog.getByLabel('Модель harness')).toHaveValue('test/second')
  await expect(
    dialog.getByLabel('Модель harness').getByRole('option'),
  ).toHaveCount(3)
})

test('switching harness replaces its model catalog and default', async ({
  page,
}) => {
  await pair(page)
  const opencode = await installedHarness(page)
  const codex = await installedHarness(page, 'codex', [
    'codex/first',
    'codex/second',
  ])
  await page.route('**/api/harnesses/discover', (route) =>
    route.fulfill({ json: [opencode, codex] }),
  )
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  await page
    .getByRole('navigation', { name: 'Разделы настроек' })
    .getByRole('button', { name: 'Группы моделей', exact: true })
    .click()
  await page
    .getByRole('region', { name: 'Группы моделей' })
    .getByRole('button', { name: '+ Новая' })
    .click()
  const dialog = page.getByRole('dialog')
  const selector = dialog.getByLabel('Harness', { exact: true })
  const model = dialog.getByLabel('Модель harness')
  await selector.selectOption(opencode.id)
  await expect(model).toHaveValue('test/first')
  await model.selectOption('test/second')
  await selector.selectOption(codex.id)
  await expect(model).toHaveValue('codex/first')
  await expect(
    model.getByRole('option', { name: 'test/second', exact: true }),
  ).toHaveCount(0)
  await dialog
    .getByLabel('Название', { exact: true })
    .fill(`Switch ${randomUUID()}`)
  await model.selectOption('codex/second')
  await dialog.getByRole('button', { name: 'Создать', exact: true }).click()
  await expect(dialog).toHaveCount(0)
  const groups = await api(page, 'GET', '/model_groups?kind=agent')
  expect(
    groups.some(
      (group: {
        members: { harness_profile_id: string; model_id: string }[]
      }) =>
        group.members.some(
          (member) =>
            member.harness_profile_id === codex.id &&
            member.model_id === 'codex/second',
        ),
    ),
  ).toBeTruthy()
})

test('missing installations and failed catalog have actionable states', async ({
  page,
}) => {
  await pair(page)
  const harness = await installedHarness(page)
  await page.route('**/api/harnesses/discover', (route) =>
    route.fulfill({ json: [] }),
  )
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  await page
    .getByRole('navigation', { name: 'Разделы настроек' })
    .getByRole('button', { name: 'Агенты', exact: true })
    .click()
  const panel = page.getByRole('region', { name: 'Установленные harness' })
  await expect(panel).toContainText('Harness не найдены')
  await page.unroute('**/api/harnesses/discover')
  await page.route('**/api/harnesses/discover', (route) =>
    route.fulfill({ json: [harness] }),
  )
  await panel.getByRole('button', { name: 'Обновить список' }).click()
  await page.route(
    `**/api/harness_profiles/${harness.id}/models/refresh*`,
    (route) =>
      route.fulfill({
        status: 422,
        json: {
          code: 'harness_catalog_unavailable',
          message: 'Проверьте вход в harness',
          details: {},
          request_id: 'fixture',
          retryable: false,
        },
      }),
  )
  await panel.getByRole('button', { name: 'OpenCode', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('alert')).toContainText('Проверьте вход')
  await expect(
    dialog.getByRole('button', { name: 'Сохранить', exact: true }),
  ).toBeDisabled()
  await expect(
    dialog.getByRole('button', { name: 'Обновить модели' }),
  ).toBeEnabled()
})
