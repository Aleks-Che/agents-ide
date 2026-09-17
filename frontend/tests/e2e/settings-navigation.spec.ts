import { expect, pair, test } from './support'

test('settings menu shows one section and retains unsaved edits between sections', async ({
  page,
}) => {
  await pair(page)
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  const menu = page.getByRole('navigation', { name: 'Разделы настроек' })
  await expect(menu.getByRole('button')).toHaveCount(6)
  await expect(
    menu.getByRole('button', { name: 'Агенты', exact: true }),
  ).toHaveAttribute('aria-pressed', 'true')
  await expect(
    page.getByRole('region', { name: 'Установленные harness' }),
  ).toBeVisible()
  await expect(
    page.getByRole('region', { name: 'Группы моделей' }),
  ).toBeHidden()
  await expect(
    page.getByRole('region', { name: 'Журнал приложения' }),
  ).toBeHidden()
  await menu
    .getByRole('button', { name: 'Совет планирования', exact: true })
    .click()
  const council = page.getByRole('region', { name: 'Совет планирования' })
  const model = council
    .getByRole('group', { name: 'Участник 1', exact: true })
    .getByLabel('Модель', { exact: true })
  await model.fill('unsaved-model-draft')
  await menu
    .getByRole('button', { name: 'LLM-подключения', exact: true })
    .click()
  await expect(council).toBeHidden()
  await expect(
    page.getByRole('region', { name: 'LLM-подключения' }),
  ).toBeVisible()
  await menu
    .getByRole('button', { name: 'Совет планирования', exact: true })
    .click()
  await expect(model).toHaveValue('unsaved-model-draft')
  await expect(page.locator('.settings-content > div:visible')).toHaveCount(1)
  await menu
    .getByRole('button', { name: 'Группы моделей', exact: true })
    .focus()
  await page.keyboard.press('Enter')
  await expect(
    page.getByRole('region', { name: 'Группы моделей' }),
  ).toBeVisible()
  await expect(page.locator('.settings-content > div:visible')).toHaveCount(1)
  await page.setViewportSize({ width: 390, height: 700 })
  const width = await page.evaluate(() => ({
    content: document.documentElement.scrollWidth,
    viewport: innerWidth,
  }))
  expect(width.content).toBeLessThanOrEqual(width.viewport)
  await menu.getByRole('button', { name: 'Журнал', exact: true }).click()
  await expect(
    page.getByRole('region', { name: 'Журнал приложения' }),
  ).toBeVisible()
  await expect(page.locator('.settings-content > div:visible')).toHaveCount(1)
})
