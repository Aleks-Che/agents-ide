import { randomUUID } from 'node:crypto'
import type { Page } from '@playwright/test'
import { test, expect, pair, api, workspace } from './support'

const graph = (suffix = '') => ({
  nodes: [
    { id: 'start', type: 'Start' },
    { id: `end${suffix}`, type: 'End' },
  ],
  edges: [{ from: 'start', to: `end${suffix}` }],
})
async function setup(page: Page) {
  await pair(page)
  const project = await api(page, 'POST', '/projects', {
    name: `Attach ${randomUUID()}`,
    workspace_path: workspace(),
  })
  const chat = await api(page, 'POST', `/projects/${project.id}/chats`, {
    title: 'Work',
  })
  const template = await api(page, 'POST', '/templates', {
    name: `Flow ${randomUUID()}`,
  })
  return { project, chat, template }
}

test('template attachment uses current saved graph and survives a failed save', async ({
  page,
}) => {
  const { project, template } = await setup(page)
  await api(page, 'POST', `/templates/${template.id}/versions`, {
    graph: graph(),
  })
  const second = await api(page, 'POST', `/templates/${template.id}/versions`, {
    graph: graph('2'),
  })
  await page.getByRole('button', { name: 'Шаблоны', exact: true }).click()
  await page
    .getByRole('button', { name: 'Создать привязку', exact: true })
    .click()
  const dialog = page.getByRole('dialog', {
    name: 'Создать привязку',
  })
  await expect(
    dialog.getByRole('button', { name: 'Создать привязку', exact: true }),
  ).toBeDisabled()
  await dialog.getByLabel('Проект', { exact: true }).selectOption(project.id)
  await dialog.getByLabel('Шаблон', { exact: true }).selectOption(template.id)
  await expect(dialog.getByLabel('Версия шаблона')).toHaveCount(0)
  await dialog.getByLabel('Название в проекте').fill('Chosen version')
  await page.route(`**/api/templates/${template.id}/bindings`, (route) =>
    route.fulfill({
      status: 409,
      json: {
        code: 'name_conflict',
        message: 'Название уже занято',
        details: {},
      },
    }),
  )
  await dialog
    .getByRole('button', { name: 'Создать привязку', exact: true })
    .click()
  await expect(dialog.getByRole('alert')).toContainText('Название уже занято')
  await expect(dialog.getByLabel('Проект', { exact: true })).toHaveValue(
    project.id,
  )
  await page.unroute(`**/api/templates/${template.id}/bindings`)
  await dialog.getByLabel('Название в проекте').fill('Ready flow')
  await dialog
    .getByRole('button', { name: 'Создать привязку', exact: true })
    .click()
  await expect(dialog).toHaveCount(0)
  await expect(
    page.getByRole('list', { name: 'Привязки проекта' }),
  ).toContainText('Ready flow')
  const bindings = await api(page, 'GET', `/bindings?project_id=${project.id}`)
  expect(bindings).toHaveLength(1)
  expect(bindings[0].version_id).toBe(second.id)
  await page.getByRole('button', { name: 'Перейти к диалогам' }).click()
  await page
    .getByRole('button', { name: 'Запустить шаблон', exact: true })
    .click()
  await expect(
    page
      .getByLabel('Шаблон проекта')
      .getByRole('option', { name: 'Ready flow' }),
  ).toHaveCount(1)
})

test('chat adds and launches a template without leaving the launch dialog', async ({
  page,
}) => {
  const { project, chat, template } = await setup(page)
  const version = await api(
    page,
    'POST',
    `/templates/${template.id}/versions`,
    { graph: graph() },
  )
  await page.reload()
  await page.getByRole('option', { name: new RegExp(project.name) }).click()
  await page.getByLabel('Новое сообщение').fill('Task draft stays here')
  await page
    .getByRole('button', { name: 'Запустить шаблон', exact: true })
    .click()
  const launch = page.getByRole('dialog', { name: 'Запустить задание' })
  await expect(launch).toContainText('В проект пока не добавлены шаблоны')
  await launch.getByRole('button', { name: 'Добавить шаблон в проект' }).click()
  let attach = page.getByRole('dialog', { name: 'Создать привязку' })
  await expect(attach).toContainText(project.name)
  await expect(attach.getByLabel('Проект', { exact: true })).toHaveCount(0)
  await attach.getByLabel('Шаблон', { exact: true }).selectOption(template.id)
  await attach
    .getByRole('button', { name: 'Создать привязку', exact: true })
    .click()
  await expect(attach).toHaveCount(0)
  const bindings = await api(page, 'GET', `/bindings?project_id=${project.id}`)
  expect(bindings).toHaveLength(1)
  expect(bindings[0].version_id).toBe(version.id)
  await expect(launch.getByLabel('Шаблон проекта')).toHaveValue(bindings[0].id)
  // Cancelling another attachment leaves the original selection and launch settings intact.
  await launch.getByRole('button', { name: 'Добавить шаблон в проект' }).click()
  attach = page.getByRole('dialog', { name: 'Создать привязку' })
  await page.keyboard.press('Escape')
  await expect(attach).toHaveCount(0)
  await expect(launch).toBeVisible()
  await expect(launch.getByLabel('Шаблон проекта')).toHaveValue(bindings[0].id)
  await launch.getByRole('button', { name: 'Запустить preflight' }).click()
  await expect(
    launch.getByRole('button', { name: 'Запустить', exact: true }),
  ).toBeEnabled()
  const started = page.waitForResponse(
    (response) =>
      response.url().endsWith('/api/runs') &&
      response.request().method() === 'POST',
  )
  await launch.getByRole('button', { name: 'Запустить', exact: true }).click()
  const response = await started
  expect(response.status()).toBe(201)
  const body = response.request().postDataJSON()
  expect(body.project_id).toBe(project.id)
  expect(body.chat_id).toBe(chat.id)
  expect(body.binding_id).toBe(bindings[0].id)
  await expect(launch).toHaveCount(0)
  await expect(
    page.getByRole('region', { name: 'Выполнение шаблона' }),
  ).toBeVisible()
  await expect(page.getByLabel('Новое сообщение')).toHaveValue(
    'Task draft stays here',
  )
})

test('unpublished template opens its constructor without creating a binding or losing the chat draft', async ({
  page,
}) => {
  const { project, template } = await setup(page)
  await api(page, 'PUT', `/templates/${template.id}/draft`, {
    expected_version: template.version,
    graph: graph(),
  })
  await page.reload()
  await page.getByRole('option', { name: new RegExp(project.name) }).click()
  await page.getByLabel('Новое сообщение').fill('Keep this task')
  await page
    .getByRole('button', { name: 'Запустить шаблон', exact: true })
    .click()
  await page.getByRole('button', { name: 'Добавить шаблон в проект' }).click()
  const attach = page.getByRole('dialog', {
    name: 'Создать привязку',
  })
  await attach.getByLabel('Шаблон', { exact: true }).selectOption(template.id)
  await expect(attach).toContainText('Шаблон ещё не сохранён для запуска')
  await expect(
    attach.getByRole('button', { name: 'Создать привязку', exact: true }),
  ).toBeDisabled()
  await attach.getByRole('button', { name: 'Открыть конструктор' }).click()
  await expect(
    page.getByRole('button', { name: 'Сохранить', exact: true }),
  ).toBeVisible()
  expect(await api(page, 'GET', `/bindings?project_id=${project.id}`)).toEqual(
    [],
  )
  await page.getByRole('button', { name: 'Закрыть конструктор' }).click()
  await page.getByRole('button', { name: 'Диалоги', exact: true }).click()
  await expect(page.getByLabel('Новое сообщение')).toHaveValue('Keep this task')
})

test('one save updates existing bindings and never offers or creates another attachment', async ({
  page,
}) => {
  const { project, template } = await setup(page)
  await api(page, 'PUT', `/templates/${template.id}/save`, {
    expected_version: template.version,
    graph: {
      ...graph(),
      input_schema: {
        type: 'object',
        properties: { task: { type: 'string' } },
      },
    },
    inputs: { task: 'Before' },
  })
  const binding = await api(
    page,
    'POST',
    `/templates/${template.id}/bindings`,
    {
      project_id: project.id,
      name: 'Existing',
      limit_overrides: { max_calls: 25 },
    },
  )
  await page.getByRole('button', { name: 'Шаблоны', exact: true }).click()
  const card = page
    .locator('li.profile-item')
    .filter({ has: page.getByText(template.name, { exact: true }) })
  await card.click({ button: 'right' })
  await page
    .getByRole('menuitem', { name: 'Редактировать', exact: true })
    .click()
  const editor = page.getByRole('dialog')
  await expect(
    editor.getByRole('button', { name: 'Сохранить', exact: true }),
  ).toHaveCount(1)
  await expect(
    editor.getByRole('button', {
      name: /Сохранить черновик|Сохранить версию|Создать привязку/,
    }),
  ).toHaveCount(0)
  await editor
    .getByRole('button', { name: 'Входы и роли', exact: true })
    .click()
  await editor
    .getByRole('group', { name: 'Начальные значения входов', exact: true })
    .getByLabel('task', { exact: true })
    .fill('After')
  await editor.getByRole('button', { name: 'Сохранить', exact: true }).click()
  await expect(editor.getByRole('status')).toContainText('Шаблон сохранён')
  await expect(
    editor.getByRole('button', { name: /Создать привязку/ }),
  ).toHaveCount(0)
  const bindings = await api(page, 'GET', `/bindings?project_id=${project.id}`)
  expect(bindings).toHaveLength(1)
  expect(bindings[0].id).toBe(binding.id)
  expect(bindings[0].version_id).not.toBe(binding.version_id)
  expect(bindings[0].limit_overrides).toEqual({ max_calls: 25 })
})
