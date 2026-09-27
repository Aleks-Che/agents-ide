import { test, expect, pair, api } from './support'

test('workspace limit can be saved, disabled and re-enabled', async ({
  page,
}) => {
  await pair(page)
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  await page
    .getByRole('navigation', { name: 'Разделы настроек' })
    .getByRole('button', { name: 'Общие', exact: true })
    .click()
  const panel = page.getByRole('region', { name: 'Общие настройки' })
  const limit = panel.getByLabel('Лимит объёма файлов, МиБ')
  const unlimited = panel.getByLabel('Без лимита', { exact: true })
  const save = panel.getByRole('button', { name: 'Сохранить общие настройки' })
  await expect(limit).toHaveValue('64')
  await limit.fill('128')
  await save.click()
  await expect(panel.getByRole('status')).toHaveText(
    'Общие настройки сохранены.',
  )
  expect(
    (await api(page, 'GET', '/settings/general'))
      .workspace_fingerprint_limit_mib,
  ).toBe(128)

  await unlimited.check()
  await expect(limit).toBeDisabled()
  await save.click()
  await expect(panel.getByRole('status')).toHaveText(
    'Общие настройки сохранены.',
  )
  expect(
    (await api(page, 'GET', '/settings/general'))
      .workspace_fingerprint_limit_mib,
  ).toBeNull()

  await page.reload()
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  await page
    .getByRole('navigation', { name: 'Разделы настроек' })
    .getByRole('button', { name: 'Общие', exact: true })
    .click()
  await expect(unlimited).toBeChecked()
  await expect(limit).toBeDisabled()
  await unlimited.uncheck()
  await limit.fill('256')
  await save.click()
  await expect(panel.getByRole('status')).toHaveText(
    'Общие настройки сохранены.',
  )
  expect(
    (await api(page, 'GET', '/settings/general'))
      .workspace_fingerprint_limit_mib,
  ).toBe(256)
})
