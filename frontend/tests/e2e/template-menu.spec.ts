import { randomUUID } from 'node:crypto'
import { test, expect, pair, api, workspace } from './support'

test('template menu copies saved content, opens attachment and removes only the copy', async ({
  page,
}) => {
  await pair(page)
  const original = await api(page, 'POST', '/templates', {
    name: `Source ${randomUUID()}`,
  })
  const version = await api(
    page,
    'POST',
    `/templates/${original.id}/versions`,
    {
      graph: {
        nodes: [
          { id: 'start', type: 'Start' },
          { id: 'end', type: 'End' },
        ],
        edges: [{ from: 'start', to: 'end' }],
      },
      inputs: { task: 'Preserved task' },
    },
  )
  await page.getByRole('button', { name: 'Шаблоны', exact: true }).click()
  const card = page
    .locator('.profile-item')
    .filter({ has: page.getByText(original.name, { exact: true }) })
  await expect(card.getByRole('button')).toHaveCount(0)
  await card.click({ button: 'right' })
  const menu = page.getByRole('menu')
  await expect(menu.getByRole('menuitem')).toHaveText([
    'Создать привязку',
    'Копировать',
    'Редактировать',
    'Переименовать',
    'Удалить',
  ])
  await menu.getByRole('menuitem', { name: 'Копировать', exact: true }).click()
  const copyName = `Copy ${randomUUID()}`
  const dialog = page.getByRole('dialog', { name: 'Копировать шаблон' })
  await dialog.getByLabel('Название шаблона').fill(copyName)
  await dialog.getByRole('button', { name: 'Копировать', exact: true }).click()
  await expect(dialog).toHaveCount(0)
  let copiedCard = page
    .locator('.profile-item')
    .filter({ has: page.getByText(copyName, { exact: true }) })
  await expect(copiedCard).toBeVisible()
  const copied = (await api(page, 'GET', '/templates')).find(
    (item: { name: string }) => item.name === copyName,
  )
  const versions = await api(page, 'GET', `/templates/${copied.id}/versions`)
  expect(versions[0].graph).toEqual(version.graph)
  expect(versions[0].inputs).toEqual(version.inputs)
  const project = await api(page, 'POST', '/projects', {
    name: 'Keep binding',
    workspace_path: workspace(),
  })
  const binding = await api(
    page,
    'POST',
    `/versions/${versions[0].id}/bindings`,
    { project_id: project.id, name: 'Keep me' },
  )

  await copiedCard.click({ button: 'right' })
  await menu
    .getByRole('menuitem', { name: 'Переименовать', exact: true })
    .click()
  const rename = page.getByRole('dialog', { name: 'Переименовать шаблон' })
  const renamed = `Renamed ${randomUUID()}`
  await expect(rename.getByLabel('Название шаблона')).toHaveValue(copyName)
  await rename.getByLabel('Название шаблона').fill('  ')
  await expect(
    rename.getByRole('button', { name: 'Сохранить', exact: true }),
  ).toBeDisabled()
  await rename.getByLabel('Название шаблона').fill(`  ${renamed}  `)
  await rename.getByRole('button', { name: 'Сохранить', exact: true }).click()
  await expect(rename).toHaveCount(0)
  await expect(copiedCard).toHaveCount(0)
  copiedCard = page.locator('.profile-item').filter({
    has: page.getByText(renamed, { exact: true }),
  })
  await expect(copiedCard).toBeVisible()
  expect((await api(page, 'GET', `/templates/${copied.id}`)).name).toBe(renamed)
  expect(await api(page, 'GET', `/templates/${copied.id}/versions`)).toEqual(
    versions,
  )
  expect(await api(page, 'GET', `/bindings/${binding.id}`)).toEqual(binding)

  await page.reload()
  await page.getByRole('button', { name: 'Шаблоны', exact: true }).click()
  await expect(copiedCard).toBeVisible()
  await expect(
    page.getByText(
      'Выберите проект слева, чтобы увидеть и редактировать его привязки.',
    ),
  ).toBeVisible()
  await page.getByRole('option', { name: new RegExp(project.name) }).click()
  await expect(
    page.getByRole('heading', { name: 'Шаблоны и привязки' }),
  ).toBeVisible()
  await expect(
    page.getByRole('list', { name: 'Привязки проекта' }),
  ).toContainText(binding.name)
  await expect(
    page.getByRole('button', { name: 'Шаблоны', exact: true }),
  ).toHaveAttribute('aria-pressed', 'true')

  await copiedCard.focus()
  await copiedCard.press('Shift+F10')
  await menu
    .getByRole('menuitem', { name: 'Создать привязку', exact: true })
    .click()
  const versionDialog = page.getByRole('dialog', {
    name: 'Создать привязку',
  })
  await expect(versionDialog.getByLabel('Шаблон', { exact: true })).toHaveValue(
    copied.id,
  )
  await page.keyboard.press('Escape')
  await expect(versionDialog).toHaveCount(0)
  await copiedCard.evaluate((element) =>
    element.dispatchEvent(
      new MouseEvent('contextmenu', {
        bubbles: true,
        cancelable: true,
        clientX: 1279,
        clientY: 719,
      }),
    ),
  )
  await expect(menu).toBeVisible()
  const bounds = await menu.boundingBox()
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(1280)
  expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(720)
  await menu.getByRole('menuitem', { name: 'Удалить', exact: true }).click()
  await expect(copiedCard).toHaveCount(0)
  await expect(card).toBeVisible()
  expect((await api(page, 'GET', `/templates/${copied.id}`)).archived).toBe(
    true,
  )
  expect((await api(page, 'GET', `/bindings/${binding.id}`)).archived).toBe(
    false,
  )
  expect((await api(page, 'GET', `/versions/${versions[0].id}`)).graph).toEqual(
    version.graph,
  )
  await page.reload()
  await page.getByRole('button', { name: 'Шаблоны', exact: true }).click()
  await expect(copiedCard).toHaveCount(0)
  await page
    .locator('.profile-item')
    .filter({ has: page.getByText('встроенный', { exact: true }) })
    .first()
    .click({ button: 'right' })
  await expect(
    menu.getByRole('menuitem', { name: 'Удалить', exact: true }),
  ).toBeDisabled()
  await expect(
    menu.getByRole('menuitem', { name: 'Редактировать', exact: true }),
  ).toBeEnabled()
  await expect(
    menu.getByRole('menuitem', { name: 'Переименовать', exact: true }),
  ).toBeDisabled()
})
