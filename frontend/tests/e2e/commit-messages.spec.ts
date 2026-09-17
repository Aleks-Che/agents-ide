import { randomUUID } from 'node:crypto'
import { api, expect, pair, test } from './support'

test('GitCommit inherits global defaults and persists independent custom generation settings', async ({
  page,
}) => {
  await pair(page)
  const globalConnection = await api(page, 'POST', '/connections', {
    name: `Global ${randomUUID()}`,
    base_url: 'http://127.0.0.1:9/v1',
    manual_models: ['global-model'],
  })
  const nodeConnection = await api(page, 'POST', '/connections', {
    name: `Custom ${randomUUID()}`,
    base_url: 'http://127.0.0.1:9/v1',
    manual_models: ['node-model'],
  })
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  await page
    .getByRole('navigation', { name: 'Разделы настроек' })
    .getByRole('button', { name: 'Общие', exact: true })
    .click()
  await expect(page.getByLabel('Промпт сообщения коммита')).toHaveValue(
    /Write a Git commit message based only on the staged diff provided/,
  )
  await page
    .getByLabel('LLM-подключение', { exact: true })
    .selectOption(globalConnection.id)
  await page.getByLabel('Модель для сообщения коммита').fill('global-model')
  await page.getByLabel('Язык сообщения коммита').selectOption('ru')
  await page.getByRole('button', { name: 'Сохранить общие настройки' }).click()
  await expect(
    page.getByRole('status').filter({ hasText: 'Общие настройки сохранены' }),
  ).toBeVisible()
  const globalPrompt = await page
    .getByLabel('Промпт сообщения коммита')
    .inputValue()
  await page.getByRole('button', { name: 'Шаблоны', exact: true }).click()
  await page.getByRole('button', { name: 'Новый шаблон', exact: true }).click()
  const name = `Commit settings ${randomUUID()}`
  await page.getByLabel('Название шаблона').fill(name)
  await page
    .getByRole('button', { name: 'Создать шаблон', exact: true })
    .click()
  await page.getByRole('button', { name: '+ GitCommit', exact: true }).click()
  await page
    .getByLabel('Сгенерировать сообщение коммита', { exact: true })
    .check()
  await expect(page.getByLabel('Модель для сообщения коммита')).toHaveValue(
    'global-model',
  )
  await expect(page.getByLabel('Промпт сообщения коммита')).toHaveValue(
    globalPrompt,
  )
  await expect(page.getByLabel('Промпт сообщения коммита')).toBeDisabled()
  await page
    .getByLabel('Собственные настройки генерации', { exact: true })
    .check()
  await page
    .getByLabel('LLM-подключение', { exact: true })
    .selectOption(nodeConnection.id)
  await page.getByLabel('Модель для сообщения коммита').fill('node-model')
  await page.getByLabel('Язык сообщения коммита').selectOption('en')
  await page
    .getByLabel('Промпт сообщения коммита')
    .fill('Custom prompt for this node')
  await page.getByLabel('Задать: temperature', { exact: true }).check()
  await page.getByLabel('temperature', { exact: true }).fill('0.3')
  await page
    .getByRole('button', { name: 'Сохранить черновик', exact: true })
    .click()
  await expect(page.getByRole('dialog').getByRole('status')).toContainText(
    'Черновик сохранён',
  )
  const template = (await api(page, 'GET', '/templates')).find(
    (item: { name: string }) => item.name === name,
  )
  const commitNode = template.draft.graph.nodes.find(
    (node: { type: string }) => node.type === 'GitCommit',
  )
  const config = commitNode.config
  expect(config.generate_message).toBe(true)
  expect(config.message_generation).toMatchObject({
    connection_id: nodeConnection.id,
    model: 'node-model',
    language: 'en',
    prompt: 'Custom prompt for this node',
    params: { temperature: 0.3 },
  })
  expect(
    (await api(page, 'GET', '/settings/general')).commit_message.prompt,
  ).toBe(globalPrompt)
  await page.getByRole('button', { name: 'Закрыть конструктор' }).click()
  await page
    .locator('.profile-item')
    .filter({ has: page.getByText(name, { exact: true }) })
    .click({ button: 'right' })
  await page
    .getByRole('menuitem', { name: 'Редактировать', exact: true })
    .click()
  await page.getByRole('button', { name: commitNode.id, exact: true }).click()
  await expect(page.getByLabel('Промпт сообщения коммита')).toHaveValue(
    'Custom prompt for this node',
  )
  await page
    .getByLabel('Собственные настройки генерации', { exact: true })
    .uncheck()
  await expect(page.getByLabel('Промпт сообщения коммита')).toHaveValue(
    globalPrompt,
  )
  await page
    .getByLabel('Сгенерировать сообщение коммита', { exact: true })
    .uncheck()
  await expect(page.getByLabel('Промпт сообщения коммита')).toHaveCount(0)
})
