import { randomUUID } from 'node:crypto'
import { test, expect, pair, api, workspace } from './support'

test('template menu copies saved content, opens versions and removes only the copy', async ({
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
    'Копировать',
    'Редактировать',
    'Версии',
    'Удалить',
  ])
  await menu.getByRole('menuitem', { name: 'Копировать', exact: true }).click()
  const copyName = `Copy ${randomUUID()}`
  const dialog = page.getByRole('dialog', { name: 'Копировать шаблон' })
  await dialog.getByLabel('Название шаблона').fill(copyName)
  await dialog.getByRole('button', { name: 'Копировать', exact: true }).click()
  await expect(dialog).toHaveCount(0)
  const copiedCard = page
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

  await copiedCard.focus()
  await copiedCard.press('Shift+F10')
  await menu.getByRole('menuitem', { name: 'Версии', exact: true }).click()
  const versionDialog = page.getByRole('dialog', {
    name: `Версии: ${copyName}`,
  })
  await expect(versionDialog.getByText('v1', { exact: true })).toBeVisible()
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
})
