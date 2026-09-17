import { createServer } from 'node:http'
import { randomUUID } from 'node:crypto'
import { test, expect, pair, api } from './support'

test('manual reasoning model is tested without a catalog and failures keep its identity', async ({
  page,
}) => {
  let fail = false
  const sentModels: string[] = []
  const server = createServer(async (request, response) => {
    response.setHeader('Content-Type', 'application/json')
    if (request.method === 'GET') {
      response.statusCode = 404
      response.end(JSON.stringify({ error: { message: 'No catalog' } }))
      return
    }
    const chunks: Buffer[] = []
    for await (const chunk of request) chunks.push(Buffer.from(chunk))
    const body = JSON.parse(Buffer.concat(chunks).toString('utf8'))
    sentModels.push(body.model)
    response.end(
      JSON.stringify({
        choices: [
          fail || body.max_tokens < 64
            ? { finish_reason: 'length', message: { role: 'assistant' } }
            : { finish_reason: 'stop', message: { content: 'OK' } },
        ],
      }),
    )
  })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  try {
    const address = server.address()
    if (!address || typeof address === 'string')
      throw new Error('Server did not bind')
    await pair(page)
    const name = `Reasoning ${randomUUID()}`
    await api(page, 'POST', '/connections', {
      name,
      base_url: `http://127.0.0.1:${address.port}/v1`,
      manual_models: ['manual-reasoner'],
    })
    await page.getByRole('button', { name: 'Настройки', exact: true }).click()
    await page
      .getByRole('navigation', { name: 'Разделы настроек' })
      .getByRole('button', { name: 'LLM-подключения', exact: true })
      .click()
    await page
      .getByRole('region', { name: 'LLM-подключения' })
      .getByRole('listitem')
      .filter({ hasText: name })
      .getByRole('button', { name: 'Параметры…' })
      .click()
    const dialog = page.getByRole('dialog', { name, exact: true })
    await dialog.getByRole('button', { name: 'Тест', exact: true }).click()
    await expect(dialog.getByRole('status')).toContainText('Тест · ok')
    await expect(dialog.getByRole('status')).toContainText(
      'Модель: manual-reasoner',
    )
    await expect(dialog.getByRole('status')).not.toContainText('0 моделей')
    await expect(dialog.getByLabel('Ручные модели')).toHaveValue(
      'manual-reasoner',
    )
    await expect(
      dialog
        .getByRole('list', { name: 'Модели подключения' })
        .getByRole('listitem'),
    ).toHaveCount(0)
    await expect(dialog).toContainText(
      'Можно использовать модель, указанную вручную',
    )
    fail = true
    await dialog.getByRole('button', { name: 'Тест', exact: true }).click()
    await expect(dialog.getByRole('status')).toContainText('Тест · failed')
    await expect(dialog.getByRole('status')).toContainText(
      'Модель: manual-reasoner',
    )
    await expect(dialog.getByRole('status')).toContainText('лимитом токенов')
    await expect(dialog.getByRole('status')).not.toContainText('0 моделей')
    expect(sentModels).toEqual(['manual-reasoner', 'manual-reasoner'])
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    )
  }
})
