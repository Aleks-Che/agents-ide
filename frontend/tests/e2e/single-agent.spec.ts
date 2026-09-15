import { randomUUID } from 'node:crypto'
import { test, expect, pair, api, workspace } from './support'

for (const kind of ['agent', 'llm'] as const) {
  test(`single ${kind}: exact node, actual preview, resource retry and stable start retry`, async ({
    page,
  }) => {
    await pair(page)
    const suffix = randomUUID().slice(0, 8)
    const project = await api(page, 'POST', '/projects', {
      name: `Single ${suffix}`,
      workspace_path: workspace(),
    })
    const chat = await api(page, 'POST', `/projects/${project.id}/chats`, {
      title: `Single chat ${suffix}`,
    })
    const profile = await api(page, 'POST', '/harness_profiles', {
      name: `Single profile ${suffix}`,
      harness_kind: 'codex',
    })
    const connection = await api(page, 'POST', '/connections', {
      name: `Single connection ${suffix}`,
      base_url: 'http://127.0.0.1:9/v1',
    })
    const field =
      kind === 'agent' ? 'harness_profile_id' : 'provider_connection_id'
    const resource = kind === 'agent' ? profile : connection
    const group = await api(page, 'POST', `/model_groups/${kind}`, {
      name: `Single group ${suffix}`,
      members: [
        { [field]: resource.id, model_id: 'disabled-single', enabled: false },
        { [field]: resource.id, model_id: 'chosen-single' },
      ],
    })
    const archived = await api(page, 'POST', `/model_groups/${kind}`, {
      name: `Obsolete ${suffix}`,
      members: [{ [field]: resource.id, model_id: 'unused-model' }],
    })
    const nodeType = kind === 'agent' ? 'AgentTask' : 'LLMRequest'
    const template = await api(page, 'POST', '/templates', {
      name: `Single template ${suffix}`,
    })
    const version = await api(
      page,
      'POST',
      `/templates/${template.id}/versions`,
      {
        graph: {
          roles: { unused_declaration: kind, shared: kind },
          nodes: [
            { id: 's', type: 'Start' },
            {
              id: 'first',
              type: nodeType,
              config: { role: 'shared', prompt: 'first task' },
            },
            {
              id: 'chosen',
              type: nodeType,
              timeout_seconds: 17,
              max_retries: 0,
              config: {
                role: 'shared',
                prompt: 'selected task',
                params: { temperature: 0.6, top_p: 0.8 },
              },
            },
            { id: 'e', type: 'End' },
          ],
          edges: [
            { from: 's', to: 'first' },
            { from: 'first', to: 'chosen' },
            { from: 'chosen', to: 'e' },
          ],
        },
      },
    )
    const binding = await api(
      page,
      'POST',
      `/versions/${version.id}/bindings`,
      {
        project_id: project.id,
        name: `Single binding ${suffix}`,
        model_selections: {
          shared: {
            kind: 'direct',
            model_id: 'inherited-single',
            [field]: resource.id,
          },
          unrelated: { kind: 'group', group_id: archived.id },
        },
      },
    )
    await api(
      page,
      'POST',
      `/model_groups/${archived.id}/archive?expected_revision=${archived.revision}`,
    )
    await page.reload()
    await page.getByRole('option', { name: new RegExp(project.name) }).click()
    await page.getByRole('option', { name: new RegExp(chat.title) }).click()
    await page.getByRole('button', { name: 'Запустить задание' }).click()
    const dialog = page.getByRole('dialog')
    const versionRoute = `**/api/versions/${version.id}`
    await page.route(versionRoute, (route) =>
      route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({
          code: 'busy',
          message: 'version load failed',
          details: {},
        }),
      }),
    )
    await dialog
      .getByLabel('Привязка', { exact: true })
      .selectOption(binding.id)

    // Loading a version must remain recoverable in both launch modes.
    await expect(dialog.getByRole('alert')).toContainText('version load failed')
    await page.unroute(versionRoute)
    await dialog
      .getByRole('button', { name: 'Повторить загрузку версии' })
      .click()
    await expect(dialog.getByText('Граф и настройки версии')).toBeVisible()
    const resourceRoute = '**/api/connections?**'
    await page.route(resourceRoute, (route) =>
      route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({
          code: 'busy',
          message: 'resource probe failed',
          details: {},
        }),
      }),
    )
    await dialog.getByLabel('Одиночный агент', { exact: true }).check()
    await expect(dialog.getByRole('alert')).toContainText(
      'resource probe failed',
    )
    await page.unroute(resourceRoute)
    await dialog
      .getByRole('button', { name: 'Повторить загрузку исполнителей' })
      .click()
    await expect(
      dialog.getByText('Не удалось загрузить исполнителей:', { exact: false }),
    ).toHaveCount(0)
    await dialog.getByLabel('Имитация (fake)').check()
    const node = dialog.getByLabel('Узел и роль')
    await expect(node.getByRole('option')).toHaveCount(3)
    await node.selectOption('chosen')
    await dialog.getByRole('button', { name: 'Запустить preflight' }).click()
    await expect(
      dialog.getByRole('button', { name: 'Запустить', exact: true }),
    ).toBeEnabled()
    const preview = dialog.getByRole('region', { name: 'Будут использованы' })
    await expect(preview).toContainText('inherited-single')
    await expect(preview).not.toContainText('unused-model')
    await expect(
      dialog.getByText('Не удалось получить итоговые настройки', {
        exact: false,
      }),
    ).toHaveCount(0)

    await dialog
      .getByRole('tab', { name: 'Прямая модель', exact: true })
      .click()
    await dialog
      .getByLabel(kind === 'agent' ? 'Harness-профиль' : 'LLM-подключение', {
        exact: true,
      })
      .selectOption(resource.id)
    await dialog.getByLabel('ID модели', { exact: true }).fill('direct-single')
    await dialog.getByRole('button', { name: 'Запустить preflight' }).click()
    await expect(preview).toContainText('direct-single')
    await expect(preview).not.toContainText('inherited-single')

    await dialog.getByRole('tab', { name: 'Группа', exact: true }).click()
    await dialog
      .getByLabel('Группа моделей', { exact: true })
      .selectOption(group.id)
    await dialog
      .getByLabel('Параметры модели (JSON)')
      .fill('{"temperature":0.2}')
    await expect(
      dialog.getByRole('button', { name: 'Запустить', exact: true }),
    ).toBeDisabled()
    const checkedResponse = page.waitForResponse((response) =>
      response.url().endsWith(`/bindings/${binding.id}/preflight`),
    )
    await dialog.getByRole('button', { name: 'Запустить preflight' }).click()
    const checked = await (await checkedResponse).json()
    expect(checked.ok).toBe(true)
    expect(Object.keys(checked.candidates)).toEqual(['chosen'])
    expect(checked.candidates.chosen[1].params).toEqual({
      temperature: 0.2,
      top_p: 0.8,
    })
    expect(checked.setting_sources['model_selections.shared']).toBe('run')
    await expect(preview).toContainText('disabled-single')
    await expect(preview).toContainText('chosen-single')
    await expect(preview).not.toContainText('direct-single')
    await page.setViewportSize({ width: 390, height: 700 })
    expect(
      await dialog.evaluate(
        (element) => element.scrollWidth <= element.clientWidth + 1,
      ),
    ).toBe(true)
    await page.screenshot({
      path: `../.local/stage9-single-${kind}-review.png`,
    })

    const requests: Record<string, unknown>[] = []
    await page.route('**/api/runs', async (route) => {
      if (route.request().method() !== 'POST') return route.continue()
      requests.push(route.request().postDataJSON())
      if (requests.length === 1) {
        expect((await route.fetch()).status()).toBe(201)
        await route.abort('failed')
      } else await route.continue()
    })
    await dialog.getByRole('button', { name: 'Запустить', exact: true }).click()
    await expect(
      dialog.getByRole('button', { name: 'Повторить тот же запуск' }),
    ).toBeEnabled()
    await dialog
      .getByRole('button', { name: 'Закрыть', exact: true })
      .last()
      .click()
    await page.reload()
    await page.getByRole('option', { name: new RegExp(project.name) }).click()
    await page.getByRole('option', { name: new RegExp(chat.title) }).click()
    await page.getByRole('button', { name: 'Запустить задание' }).click()
    await dialog
      .getByRole('button', { name: 'Повторить тот же запуск' })
      .click()
    await expect(dialog.getByRole('heading', { name: /Run ·/ })).toBeVisible()
    expect(requests).toHaveLength(2)
    expect(requests[0]).toEqual(requests[1])
    expect(requests[0]).toMatchObject({
      single_agent: {
        role: 'shared',
        node_id: 'chosen',
        selection: { kind: 'group', group_id: group.id },
      },
      trusted_execution_hash: checked.execution_hash,
    })
    expect(await api(page, 'GET', `/runs?chat_id=${chat.id}`)).toHaveLength(1)
  })
}
