import { execFileSync } from 'node:child_process'
import path from 'node:path'
import { randomUUID } from 'node:crypto'
import { test, expect, pair, api, workspace } from './support'

test('loop counters and live iteration buttons persist in chat and graph', async ({
  page,
}) => {
  test.setTimeout(60000)
  await pair(page)
  await page.setViewportSize({ width: 1450, height: 1050 })
  const project = await api(page, 'POST', '/projects', {
    name: `Loops ${randomUUID()}`,
    workspace_path: workspace(),
  })
  const chat = await api(page, 'POST', `/projects/${project.id}/chats`, {
    title: 'Loop controls',
  })
  const profile = await api(page, 'POST', '/harness_profiles', {
    name: `Loop ${randomUUID()}`,
    harness_kind: 'codex',
  })
  const template = await api(page, 'POST', '/templates', {
    name: `Loop ${randomUUID()}`,
  })
  const version = await api(
    page,
    'POST',
    `/templates/${template.id}/versions`,
    {
      graph: {
        nodes: [
          { id: 'start', type: 'Start' },
          {
            id: 'first',
            type: 'AgentTask',
            label: 'Implementation',
            config: {
              prompt: 'go',
              model_selection: {
                kind: 'direct',
                harness_profile_id: profile.id,
                model_id: 'test',
              },
            },
          },
          {
            id: 'route',
            type: 'Condition',
            label: 'Check',
            expression: { const: false },
          },
          { id: 'end', type: 'End' },
        ],
        edges: [
          { from: 'start', to: 'first' },
          { from: 'first', to: 'route' },
          {
            id: 'repair',
            from: 'route',
            to: 'first',
            when: 'false',
            loop: { id: 'repair', max_iterations: 3 },
          },
          { from: 'route', to: 'end', when: 'true' },
          { from: 'route', to: 'end', when: 'unknown' },
        ],
      },
    },
  )
  const binding = await api(page, 'POST', `/versions/${version.id}/bindings`, {
    project_id: project.id,
    name: 'Loops',
  })
  const run = await api(page, 'POST', '/runs', {
    project_id: project.id,
    chat_id: chat.id,
    binding_id: binding.id,
    execution_mode: 'simulated',
    message: 'go',
    idempotency_key: randomUUID(),
  })
  const checkpoint = (mode: string) => {
    execFileSync(
      path.resolve(
        '../backend/.venv',
        process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
      ),
      [
        path.resolve('tests/e2e/seed_chat_progress.py'),
        process.env.AGENTS_IDE_E2E_DATA_DIR!,
        run.id,
        mode,
      ],
      { windowsHide: true },
    )
  }
  checkpoint('first')
  checkpoint('loop-counts')
  await page.reload()
  await page.getByRole('option', { name: new RegExp(project.name) }).click()
  const progress = page.getByRole('region', { name: 'Выполнение шаблона' })
  const loop = progress.getByRole('article', { name: 'Цикл repair' })
  await expect(loop).toContainText('Пройдено 2')
  await expect(loop).toContainText('Осталось 1')
  await loop
    .getByRole('button', { name: 'Уменьшить остаток цикла repair' })
    .click()
  await expect(loop).toContainText('Осталось 0')
  await expect(
    loop.getByRole('button', { name: 'Уменьшить остаток цикла repair' }),
  ).toBeDisabled()
  for (const remaining of [1, 2]) {
    await loop
      .getByRole('button', { name: 'Увеличить остаток цикла repair' })
      .click()
    await expect(loop).toContainText(`Осталось ${remaining}`)
  }
  await page.reload()
  await page.getByRole('option', { name: new RegExp(project.name) }).click()
  await expect(loop).toContainText('Пройдено 2')
  await expect(loop).toContainText('Осталось 2')
  const current = await api(page, 'GET', `/runs/${run.id}`)
  expect(current.state).toBe('running')
  expect(current.runtime.loop_counts.repair).toBe(2)
  expect(current.runtime.loop_limits.repair).toBe(4)
  await page.screenshot({
    path: '../.local/loop-controls-chat.png',
    fullPage: true,
  })
  await progress
    .getByRole('button', { name: 'Подробности', exact: true })
    .click()
  const dialog = page.getByRole('dialog')
  await expect(
    dialog.getByRole('article', { name: 'Цикл repair' }),
  ).toContainText('Осталось 2')
  await expect(
    dialog.getByRole('group', { name: 'Edge from route to first' }),
  ).toContainText('цикл repair: пройдено 2, осталось 2')
  await dialog
    .getByRole('button', { name: 'Уменьшить остаток цикла repair' })
    .click()
  await expect(
    dialog.getByRole('article', { name: 'Цикл repair' }),
  ).toContainText('Осталось 1')
  await expect(
    dialog.getByRole('group', { name: 'Edge from route to first' }),
  ).toContainText('цикл repair: пройдено 2, осталось 1')
  await dialog.getByRole('button', { name: 'Закрыть экран Run' }).click()
  await expect(dialog).toHaveCount(0)
  await expect(progress).toHaveCount(1)
  checkpoint('loop-next')
  await expect(progress).toHaveCount(1)
  const navigation = progress.getByRole('navigation', {
    name: 'Этапы выполнения',
  })
  await expect(loop).toContainText('Пройдено 3')
  await expect(navigation.getByRole('button', { name: /start/ })).toContainText(
    'Завершён',
  )
  await expect(
    navigation.getByRole('button', { name: /Implementation/ }),
  ).toContainText('Выполняется')
  await expect(navigation.getByRole('button', { name: /Check/ })).toContainText(
    'Ожидает',
  )
  await expect(navigation.locator('.chat-activity-spinner')).toHaveCount(1)
  await page.reload()
  await page.getByRole('option', { name: new RegExp(project.name) }).click()
  await expect(navigation.getByRole('button', { name: /Check/ })).toContainText(
    'Ожидает',
  )
  await expect(navigation.getByRole('button', { name: /start/ })).toContainText(
    'Завершён',
  )
  await expect(
    progress.locator('.stage-heading .chat-activity-spinner'),
  ).toBeVisible()
})
