import { test, expect, pair, workspace, api } from './support'

test('history beyond 200 messages loads backwards and shows newly sent messages', async ({
  page,
}) => {
  test.setTimeout(60_000)
  await pair(page)
  const project = await api(page, 'POST', '/projects', {
    name: 'Long history',
    workspace_path: workspace(),
  })
  const chat = await api(page, 'POST', `/projects/${project.id}/chats`, {
    title: 'History 205',
  })
  const session = await (await page.request.get('/api/auth/session')).json()
  for (let index = 0; index < 205; index++) {
    const response = await page.request.post(`/api/chats/${chat.id}/messages`, {
      data: { content: `Message ${index}`, role: 'user' },
      headers: {
        'X-CSRF-Token': session.csrf_token,
        Origin: 'http://127.0.0.1:18767',
      },
    })
    expect(response.status()).toBe(201)
  }
  await page.reload()
  await page.getByRole('option', { name: /Long history/ }).click()
  const messages = page.getByRole('list', { name: 'Сообщения диалога' })
  await expect(messages.getByRole('listitem')).toHaveCount(200)
  await expect(messages.getByText('Message 204', { exact: true })).toBeVisible()
  await page
    .getByRole('button', { name: 'Загрузить более ранние сообщения' })
    .click()
  await expect(messages.getByRole('listitem')).toHaveCount(205)
  await page.getByLabel('Новое сообщение').fill('Newest message')
  await page.getByRole('button', { name: 'Отправить', exact: true }).click()
  await expect(
    messages.getByText('Newest message', { exact: true }),
  ).toBeVisible()
  await expect(messages.getByRole('listitem')).toHaveCount(206)
})

test('project probe, rename, isolated chat drafts and archive the last chat', async ({
  page,
}) => {
  await pair(page)
  const directory = workspace()
  await page.getByRole('button', { name: 'Новый проект' }).click()
  let dialog = page.getByRole('dialog', { name: 'Новый проект' })
  await dialog.getByLabel('Название').fill('Demo workspace')
  await dialog.getByLabel('Путь к рабочему каталогу').fill(directory)
  await dialog.getByRole('button', { name: 'Проверить' }).click()
  await expect(dialog.locator('.probe-summary code')).toHaveText(directory)
  await dialog
    .getByLabel('Путь к рабочему каталогу')
    .fill(`${directory}-missing`)
  await expect(dialog.locator('.probe-summary')).toHaveCount(0)
  await dialog.getByRole('button', { name: 'Проверить' }).click()
  await expect(dialog.getByRole('alert')).toBeVisible()
  await dialog.getByLabel('Путь к рабочему каталогу').fill(directory)
  await dialog.getByRole('button', { name: 'Создать' }).click()
  await expect(
    page.getByRole('option', { name: /Demo workspace/ }),
  ).toBeVisible()
  await page.getByRole('button', { name: 'Переименовать проект' }).click()
  dialog = page.getByRole('dialog', { name: 'Переименовать проект' })
  await dialog.getByLabel('Название').fill('Renamed workspace')
  await dialog.getByRole('button', { name: 'Сохранить' }).click()
  await expect(
    page.getByRole('option', { name: /Renamed workspace/ }),
  ).toBeVisible()

  for (const title of ['Первый', 'Второй']) {
    await page.getByRole('button', { name: 'Новый диалог' }).click()
    const create = page.getByRole('dialog', { name: 'Новый диалог' })
    await create.getByLabel('Название').fill(title)
    await create.getByRole('button', { name: 'Создать' }).click()
    await expect(
      page.getByRole('heading', { name: title, exact: true }),
    ).toBeVisible()
    await page.getByLabel('Новое сообщение').fill(`Черновик ${title}`)
  }
  await page.getByRole('option', { name: /Первый/ }).click()
  await expect(page.getByLabel('Новое сообщение')).toHaveValue(
    'Черновик Первый',
  )
  await page.getByRole('button', { name: 'Отправить', exact: true }).click()
  await expect(
    page.getByRole('list', { name: 'Сообщения диалога' }),
  ).toContainText('Черновик Первый')
  await page.getByRole('button', { name: 'Архивировать диалог' }).click()
  await expect(
    page.getByRole('heading', { name: 'Второй', exact: true }),
  ).toBeVisible()
  await expect(page.getByLabel('Новое сообщение')).toHaveValue(
    'Черновик Второй',
  )
  await page.getByRole('button', { name: 'Архивировать диалог' }).click()
  await expect(
    page.getByRole('heading', { name: 'Диалог не выбран' }),
  ).toBeVisible()
  await page.getByRole('button', { name: 'Выйти', exact: true }).click()
  await expect(
    page.getByRole('heading', { name: 'Подключить этот браузер' }),
  ).toBeVisible()
})

test('late message response does not clear the next chat draft', async ({
  page,
}) => {
  await pair(page)
  const project = await api(page, 'POST', '/projects', {
    name: 'Async chats',
    workspace_path: workspace(),
  })
  const a = await api(page, 'POST', `/projects/${project.id}/chats`, {
    title: 'Async A',
  })
  await api(page, 'POST', `/projects/${project.id}/chats`, { title: 'Async B' })
  await page.reload()
  await page.getByRole('option', { name: /Async chats/ }).click()
  await page.getByRole('option', { name: /Async A/ }).click()
  let release!: () => void
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  let dispatched!: () => void
  const pending = new Promise<void>((resolve) => {
    dispatched = resolve
  })
  await page.route(`**/api/chats/${a.id}/messages`, async (route) => {
    if (route.request().method() !== 'POST') return route.continue()
    const response = await route.fetch()
    dispatched()
    await gate
    await route.fulfill({ response })
  })
  await page.getByLabel('Новое сообщение').fill('For A only')
  await page.getByRole('button', { name: 'Отправить', exact: true }).click()
  await pending
  await page.getByRole('option', { name: /Async B/ }).click()
  await page.getByLabel('Новое сообщение').fill('Keep B draft')
  release()
  await expect(page.getByLabel('Новое сообщение')).toHaveValue('Keep B draft')
  await page.getByRole('option', { name: /Async A/ }).click()
  await expect(
    page.getByRole('list', { name: 'Сообщения диалога' }),
  ).toContainText('For A only')
  await expect(page.getByLabel('Новое сообщение')).toHaveValue('')
})
