import { randomUUID } from 'node:crypto'
import { api, expect, pair, test, workspace } from './support'
import { installedHarness } from './harness-fixture'

test('one save persists new candidates, metadata and painted schedules; header stays visible', async ({
  page,
}) => {
  await pair(page)
  await page.setViewportSize({ width: 1100, height: 750 })
  const connection = await api(page, 'POST', '/connections', {
    name: randomUUID(),
    base_url: 'http://127.0.0.1:9/v1',
  })
  const group = await api(page, 'POST', '/model_groups/llm', {
    name: randomUUID(),
    members: [{ provider_connection_id: connection.id, model_id: 'first' }],
  })
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  await page
    .getByRole('navigation', { name: 'Разделы настроек' })
    .getByRole('button', { name: 'Группы моделей', exact: true })
    .click()
  const panel = page.getByRole('region', { name: 'Группы моделей' })
  await panel.getByRole('tab', { name: 'llm', exact: true }).click()
  await panel
    .locator('.profile-item')
    .filter({ hasText: group.name })
    .getByRole('button', { name: 'Параметры…' })
    .click()
  const dialog = page.getByRole('dialog')
  await expect(
    dialog.getByRole('button', { name: 'Сохранить кандидатов', exact: true }),
  ).toHaveCount(0)
  await dialog
    .getByLabel('Название', { exact: true })
    .fill(`${group.name} saved`)
  await dialog.getByLabel('Описание').fill('All settings together')
  await dialog.getByRole('button', { name: '+ Добавить' }).click()
  await dialog.getByLabel('ID модели').nth(1).fill('backup')
  await dialog
    .getByRole('button', { name: 'Параметры', exact: true })
    .first()
    .click()
  await dialog.getByLabel('Использовать расписание').check()
  await dialog.getByLabel('Часовой пояс').selectOption('UTC')
  const grid = dialog.getByRole('table', {
    name: 'Разрешённые часы использования модели',
  })
  await expect(grid.getByRole('button')).toHaveCount(168)
  await grid
    .getByRole('button', { name: 'Пн, 00:00–01:00', exact: true })
    .click()
  await expect(
    grid.getByRole('button', { name: 'Вт, 00:00–01:00', exact: true }),
  ).toHaveAttribute('aria-pressed', 'true')
  const first = grid.getByRole('button', {
    name: 'Пн, 01:00–02:00',
    exact: true,
  })
  await first.hover()
  await page.mouse.down()
  await grid
    .getByRole('button', { name: 'Пн, 02:00–03:00', exact: true })
    .hover()
  await page.mouse.up()
  await dialog.getByLabel('Одно расписание для всех дней').check()
  await expect(grid.getByRole('button')).toHaveCount(24)
  await expect(
    grid.getByRole('button', { name: 'Все дни, 02:00–03:00', exact: true }),
  ).toHaveAttribute('aria-pressed', 'false')
  await dialog
    .getByRole('button', { name: 'Можно использовать', exact: true })
    .click()
  await grid
    .getByRole('button', { name: 'Все дни, 00:00–01:00', exact: true })
    .click()
  await dialog.getByLabel('Использовать расписание').uncheck()
  await expect(grid).toHaveCount(0)
  await dialog.getByLabel('Использовать расписание').check()
  await dialog.locator('.group-settings-dialog').evaluate((element) => {
    element.scrollTop = element.scrollHeight
  })
  const header = await dialog
    .locator('.group-settings-dialog > header')
    .boundingBox()
  const bounds = await dialog.boundingBox()
  expect(header!.y).toBeGreaterThanOrEqual(bounds!.y - 1)
  expect(header!.y + header!.height).toBeLessThan(bounds!.y + bounds!.height)
  await dialog.getByRole('button', { name: 'Сохранить', exact: true }).click()
  await expect(dialog.getByRole('status')).toHaveText('Группа сохранена.')
  const saved = await api(page, 'GET', `/model_groups/${group.id}`)
  expect(saved.name).toBe(`${group.name} saved`)
  expect(saved.description).toBe('All settings together')
  expect(saved.members.map((m: { model_id: string }) => m.model_id)).toEqual([
    'first',
    'backup',
  ])
  expect(saved.members[0].schedule).toMatchObject({
    enabled: true,
    same_every_day: true,
    timezone: 'UTC',
  })
  expect(saved.members[0].schedule.days[0].slice(0, 4)).toEqual([
    true,
    false,
    false,
    true,
  ])
  await dialog.getByRole('button', { name: 'Закрыть настройки группы' }).click()
  await panel
    .locator('.profile-item')
    .filter({ hasText: saved.name })
    .getByRole('button', { name: 'Параметры…' })
    .click()
  await dialog.getByRole('button', { name: /Параметры · расписание/ }).click()
  await expect(dialog.getByLabel('Одно расписание для всех дней')).toBeChecked()
  await expect(
    grid.getByRole('button', { name: 'Все дни, 01:00–02:00', exact: true }),
  ).toHaveAttribute('aria-pressed', 'false')
  await dialog.getByLabel('Одно расписание для всех дней').uncheck()
  await expect(
    grid.getByRole('button', { name: 'Вс, 01:00–02:00', exact: true }),
  ).toHaveAttribute('aria-pressed', 'false')
  await page.screenshot({ path: '../.local/model-schedule-editor.png' })
})

test('Council settings persist and populate planning dialog', async ({
  page,
}) => {
  await pair(page)
  const previous = await api(page, 'GET', '/settings/planning-council')
  const harness = await installedHarness(page)
  const connection = await api(page, 'POST', '/connections', {
    name: randomUUID(),
    base_url: 'http://127.0.0.1:9/v1',
  })
  const project = await api(page, 'POST', '/projects', {
    name: randomUUID(),
    workspace_path: workspace(),
  })
  await api(page, 'POST', `/projects/${project.id}/chats`, {
    title: 'Planning defaults',
  })
  await page.reload()
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  await page
    .getByRole('navigation', { name: 'Разделы настроек' })
    .getByRole('button', { name: 'Совет планирования', exact: true })
    .click()
  const panel = page.getByRole('region', { name: 'Совет планирования' })
  await panel.getByLabel('Число участников').selectOption('2')
  const first = panel.getByRole('group', { name: 'Участник 1', exact: true })
  await first.getByLabel('Подтип').selectOption('agent')
  await first.getByLabel('Профиль harness').selectOption(harness.id)
  await first.getByLabel('Модель', { exact: true }).fill('test/first')
  for (const [title, model] of [
    ['Участник 2', 'second'],
    ['Объединяющий', 'merge'],
  ]) {
    const member = panel.getByRole('group', { name: title, exact: true })
    await member
      .getByRole('combobox', { name: 'Подключение', exact: true })
      .selectOption(connection.id)
    await member.getByLabel('Модель', { exact: true }).fill(model)
  }
  await panel.getByRole('button', { name: 'Сохранить состав совета' }).click()
  await expect(panel.getByRole('status')).toHaveText('Состав совета сохранён.')
  try {
    await page.reload()
    await page.getByRole('button', { name: 'Настройки', exact: true }).click()
    await page
      .getByRole('navigation', { name: 'Разделы настроек' })
      .getByRole('button', { name: 'Совет планирования', exact: true })
      .click()
    await expect(first.getByLabel('Модель', { exact: true })).toHaveValue(
      'test/first',
    )
    await page.getByRole('option', { name: new RegExp(project.name) }).click()
    await page
      .getByRole('button', { name: 'Составить план несколькими моделями' })
      .click()
    const dialog = page.getByRole('dialog')
    await expect(dialog.getByLabel('Число участников')).toHaveValue('2')
    await expect(
      dialog
        .getByRole('group', { name: 'Участник 1', exact: true })
        .getByLabel('Модель', { exact: true }),
    ).toHaveValue('test/first')
    await expect(
      dialog
        .getByRole('group', { name: 'Объединяющий', exact: true })
        .getByLabel('Модель', { exact: true }),
    ).toHaveValue('merge')
  } finally {
    const current = await api(page, 'GET', '/settings/planning-council')
    await api(page, 'PUT', '/settings/planning-council', {
      ...previous,
      revision: current.revision,
    })
  }
})

test('existing published templates and built-in presets open in constructor', async ({
  page,
}) => {
  await pair(page)
  const template = await api(page, 'POST', '/templates', {
    name: `Existing ${randomUUID()}`,
  })
  await api(page, 'POST', `/templates/${template.id}/versions`, {
    graph: {
      nodes: [
        { id: 's', type: 'Start' },
        { id: 'e', type: 'End' },
      ],
      edges: [{ from: 's', to: 'e' }],
    },
  })
  await page.getByRole('button', { name: 'Шаблоны', exact: true }).click()
  const item = page
    .locator('.profile-item')
    .filter({ has: page.getByText(template.name, { exact: true }) })
  await item.click({ button: 'right' })
  await page
    .getByRole('menuitem', { name: 'Редактировать', exact: true })
    .click()
  await expect(page.locator('.react-flow__node')).toHaveCount(2)
  await page.getByRole('button', { name: '+ Condition', exact: true }).click()
  await page
    .getByRole('button', { name: 'Сохранить черновик', exact: true })
    .click()
  await expect
    .poll(
      async () =>
        (await api(page, 'GET', `/templates/${template.id}`)).draft.graph.nodes
          .length,
    )
    .toBe(3)
  await page.getByRole('button', { name: 'Закрыть конструктор' }).click()
  await item.click({ button: 'right' })
  await page
    .getByRole('menuitem', { name: 'Редактировать', exact: true })
    .click()
  await expect(page.locator('.react-flow__node')).toHaveCount(3)
  await page.getByRole('button', { name: 'Закрыть конструктор' }).click()
  const preset = page
    .locator('.profile-item')
    .filter({ has: page.getByText('встроенный', { exact: true }) })
    .first()
  await preset.click({ button: 'right' })
  await page
    .getByRole('menuitem', { name: 'Редактировать', exact: true })
    .click()
  const copiedName = `Edited preset ${randomUUID()}`
  await page.getByLabel('Название шаблона').fill(copiedName)
  await page
    .getByRole('button', { name: 'Создать копию и редактировать' })
    .click()
  await expect(
    page.getByRole('heading', { name: copiedName, exact: true }),
  ).toBeVisible()
  await expect(page.locator('.react-flow__node').first()).toBeVisible()
})
