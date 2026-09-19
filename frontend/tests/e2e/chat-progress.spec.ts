import { execFileSync } from 'node:child_process'
import path from 'node:path'
import { randomUUID } from 'node:crypto'
import { test, expect, pair, api, workspace } from './support'

function checkpoint(runId: string, mode: string) {
  execFileSync(
    path.resolve(
      '../backend/.venv',
      process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
    ),
    [
      path.resolve('tests/e2e/seed_chat_progress.py'),
      process.env.AGENTS_IDE_E2E_DATA_DIR!,
      runId,
      mode,
    ],
    { windowsHide: true },
  )
}

test('chat streams stages, sends attempt-scoped replies and controls STOP and START', async ({
  page,
}) => {
  test.setTimeout(60000)
  await pair(page)
  await page.setViewportSize({ width: 1560, height: 1100 })
  const project = await api(page, 'POST', '/projects', {
    name: `Progress ${randomUUID()}`,
    workspace_path: workspace(),
  })
  await api(page, 'POST', `/projects/${project.id}/chats`, {
    title: 'Idle chat',
  })
  const chat = await api(page, 'POST', `/projects/${project.id}/chats`, {
    title: 'Live chat',
  })
  const profile = await api(page, 'POST', '/harness_profiles', {
    name: `Profile ${randomUUID()}`,
    harness_kind: 'codex',
  })
  const template = await api(page, 'POST', '/templates', {
    name: `Progress template ${randomUUID()}`,
  })
  const group = await api(page, 'POST', '/model_groups/agent', {
    name: `Recovery ${randomUUID()}`,
    members: [
      { harness_profile_id: profile.id, model_id: 'test' },
      { harness_profile_id: profile.id, model_id: 'backup' },
    ],
  })
  const selection = { kind: 'group', group_id: group.id }
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
            config: { prompt: 'go', model_selection: selection },
          },
          {
            id: 'second',
            type: 'AgentTask',
            label: 'Review',
            config: { prompt: 'review', model_selection: selection },
          },
          { id: 'end', type: 'End' },
        ],
        edges: [
          { from: 'start', to: 'first' },
          { from: 'first', to: 'second' },
          { from: 'second', to: 'end' },
        ],
      },
    },
  )
  const binding = await api(page, 'POST', `/versions/${version.id}/bindings`, {
    project_id: project.id,
    name: 'Progress',
  })
  for (const mode of ['historical-failed', 'historical-waiting']) {
    const historical = await api(page, 'POST', '/runs', {
      project_id: project.id,
      chat_id: chat.id,
      binding_id: binding.id,
      execution_mode: 'simulated',
      message: 'previous run',
      idempotency_key: randomUUID(),
    })
    checkpoint(historical.id, mode)
  }
  const run = await api(page, 'POST', '/runs', {
    project_id: project.id,
    chat_id: chat.id,
    binding_id: binding.id,
    execution_mode: 'simulated',
    message: 'go',
    idempotency_key: randomUUID(),
  })
  checkpoint(run.id, 'first')
  await page.reload()
  await page.getByRole('option', { name: new RegExp(project.name) }).click()
  const progress = page.getByRole('region', { name: 'Выполнение шаблона' })
  await expect(progress).toBeVisible()
  const chatOption = page.getByRole('option', { name: /Live chat/ })
  const activity = chatOption.getByRole('status', {
    name: 'В диалоге выполняется процесс',
  })
  await expect(activity).toBeVisible()
  const projectOption = page.getByRole('option', {
    name: new RegExp(project.name),
  })
  const projectActivity = projectOption.getByRole('status', {
    name: 'В проекте выполняется процесс',
  })
  const projectAttention = projectOption.getByRole('status', {
    name: 'В проекте требуется внимание: ошибка или запрос',
  })
  const chatAttention = chatOption.getByRole('status', {
    name: 'В диалоге требуется внимание: ошибка или запрос',
  })
  await expect(projectActivity).toBeVisible()
  await expect(projectAttention).toHaveCount(0)
  await expect(chatAttention).toHaveCount(0)
  await expect(activity.locator('svg')).toHaveCSS(
    'animation-name',
    'chat-activity-spin',
  )
  await expect(progress.locator('.stage-card')).toHaveCount(1)
  await expect(progress.locator('.stage-heading')).toHaveText(
    '2. ImplementationВыполняется',
  )
  await expect(progress.locator('button.stage-heading')).toHaveCount(0)
  const idleChat = page.getByRole('option', { name: /Idle chat/ })
  await expect(idleChat.getByRole('status')).toHaveCount(0)
  await idleChat.click()
  await expect(
    page.getByRole('heading', { name: 'Idle chat', exact: true }),
  ).toBeVisible()
  await expect(activity).toBeVisible()
  await chatOption.click()
  await expect(progress.getByRole('log')).toContainText(
    'Checking the first files',
  )
  const log = progress.getByRole('log')
  const bottomGap = () =>
    log.evaluate((el) => el.scrollHeight - el.clientHeight - el.scrollTop)
  checkpoint(run.id, 'stream-first')
  await expect(log).toContainText('stream-first line 59')
  await expect.poll(bottomGap).toBeLessThan(2)
  await log.evaluate((el) => {
    el.scrollTop = 0
  })
  const follow = progress.getByRole('button', {
    name: 'К последним сообщениям',
  })
  await expect(follow).toBeVisible()
  checkpoint(run.id, 'stream-paused')
  await expect(log).toContainText('stream-paused line 59')
  expect(await log.evaluate((el) => el.scrollTop)).toBe(0)
  await follow.click()
  await expect.poll(bottomGap).toBeLessThan(2)
  checkpoint(run.id, 'stream-resumed')
  await expect(log).toContainText('stream-resumed line 59')
  await expect.poll(bottomGap).toBeLessThan(2)
  await page.setViewportSize({ width: 1366, height: 768 })
  await expect.poll(bottomGap).toBeLessThan(2)
  const sections = page.getByRole('navigation', {
    name: 'Разделы',
    exact: true,
  })
  const beforeScroll = await sections.boundingBox()
  expect(beforeScroll!.y + beforeScroll!.height).toBeLessThan(768)
  const main = page.getByRole('main')
  await main.evaluate((el) => {
    el.scrollTop = el.scrollHeight
  })
  expect(await main.evaluate((el) => el.scrollTop)).toBeGreaterThan(0)
  expect(await sections.boundingBox()).toEqual(beforeScroll)
  expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBe(
    768,
  )
  await page.screenshot({ path: '../.local/chat-sidebar-fixed.png' })
  await main.evaluate((el) => {
    el.scrollTop = 0
  })
  await page.setViewportSize({ width: 1560, height: 1100 })
  checkpoint(run.id, 'question')
  checkpoint(run.id, 'tools')
  await expect(
    progress
      .getByRole('log')
      .getByText('read · завершён · status.md', { exact: true }),
  ).toHaveCount(1)
  await expect(progress.getByRole('log')).not.toContainText(
    'read · выполняется',
  )
  checkpoint(run.id, 'repeated-tools')
  await expect(
    progress.getByRole('log').getByText('(4) Инструмент', { exact: true }),
  ).toHaveCount(1)
  await expect(
    progress.getByRole('log').getByText('Инструмент', { exact: true }),
  ).toHaveCount(0)
  await expect(progress.getByText('Агент ожидает ответа')).toBeVisible()
  await expect(projectAttention).toBeVisible()
  await expect(chatAttention).toBeVisible()
  await expect(activity).toHaveCount(0)
  await page.reload()
  await page.getByRole('option', { name: new RegExp(project.name) }).click()
  await progress.getByLabel('Which file should I check?').fill('README.md')
  await progress
    .getByRole('button', { name: 'Отправить агенту', exact: true })
    .click()
  await expect(progress).toContainText('Ожидает передачи агенту')
  checkpoint(run.id, 'deliver')
  await expect(progress.getByText('Агент ожидает ответа')).toHaveCount(0)
  await expect(chatAttention).toHaveCount(0)
  await expect(projectAttention).toHaveCount(0)
  await expect(projectActivity).toBeVisible()
  await progress
    .getByLabel('Сообщение текущему агенту')
    .fill('Please also check README')
  await progress
    .getByRole('button', { name: 'Отправить агенту', exact: true })
    .click()
  await expect(progress).toContainText('Ожидает передачи агенту')
  checkpoint(run.id, 'deliver')
  await expect(progress).toContainText('Передано агенту')
  const commands = await api(page, 'GET', `/runs/${run.id}/commands`)
  expect(commands).toHaveLength(2)
  expect(commands[0].status).toBe('applied')
  checkpoint(run.id, 'permission')
  await expect(progress.getByText('Агент запрашивает разрешение')).toBeVisible()
  await expect(chatAttention).toBeVisible()
  await expect(projectAttention).toBeVisible()
  await page.reload()
  await page.getByRole('option', { name: new RegExp(project.name) }).click()
  const permissionReply = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' &&
      response.url().endsWith(`/runs/${run.id}/commands`),
  )
  await progress
    .getByRole('button', { name: 'Разрешить один раз', exact: true })
    .click()
  const approval = await permissionReply
  expect(approval.status()).toBe(200)
  expect(approval.request().postDataJSON().payload).toMatchObject({
    permission_id: 'p1',
    permission_reply: 'once',
  })
  checkpoint(run.id, 'deliver')
  await expect(progress.getByText('Агент запрашивает разрешение')).toHaveCount(
    0,
  )
  checkpoint(run.id, 'second')
  await expect(progress.locator('.stage-card')).toHaveCount(1)
  await expect(progress.locator('.stage-heading')).toContainText('3. Review')
  await expect(progress.getByRole('log')).toContainText('Reviewing the result')
  const navigation = progress.getByRole('navigation', {
    name: 'Этапы выполнения',
  })
  await navigation.getByRole('button', { name: /Implementation/ }).click()
  await expect(progress.locator('.stage-card')).toHaveCount(1)
  await expect(progress.locator('.stage-heading')).toContainText(
    '2. Implementation',
  )
  await expect(progress.getByRole('log')).toContainText(
    'Please also check README',
  )
  await expect(progress.getByLabel('Сообщение текущему агенту')).toHaveCount(0)
  await navigation.getByRole('button', { name: /Review/ }).click()
  await progress
    .getByRole('button', { name: 'Приостановить выполнение' })
    .click()
  await expect(progress).toContainText('Пауза запрошена')
  await expect(
    progress.getByRole('button', { name: 'Приостановить выполнение' }),
  ).toBeDisabled()
  checkpoint(run.id, 'paused')
  await expect(progress).toContainText('На паузе')
  await expect(activity).toHaveCount(0)
  await expect(
    progress.getByRole('button', { name: 'Продолжить выполнение' }),
  ).toBeEnabled()
  await progress
    .getByRole('button', { name: 'Остановить выполнение', exact: true })
    .click()
  await expect(progress).toContainText(
    'Остановлен; START начнёт текущий этап заново',
  )
  await expect(
    progress.getByRole('button', { name: 'Продолжить выполнение' }),
  ).toBeEnabled()
  await progress.getByRole('button', { name: 'Продолжить выполнение' }).click()
  await expect(progress).toContainText('В очереди исполнителя')
  await expect(activity).toBeVisible()
  await page.screenshot({ path: '../.local/chat-progress.png', fullPage: true })
  await page.reload()
  await page.getByRole('option', { name: new RegExp(project.name) }).click()
  await expect(progress).toBeVisible()
  await progress
    .getByRole('navigation', { name: 'Этапы выполнения' })
    .getByRole('button', { name: /Implementation/ })
    .click()
  await expect(progress.getByRole('log')).toContainText(
    'Please also check README',
  )
  checkpoint(run.id, 'waiting-recovery')
  const warning = progress.locator('.stage-waiting')
  await expect(warning).toBeVisible()
  await expect(projectAttention).toBeVisible()
  await expect(chatAttention).toBeVisible()
  await expect(activity).toHaveCount(0)
  await navigation.getByRole('button', { name: /Review/ }).click()
  await expect(
    progress.locator('.stage-heading').filter({ hasText: '3. Review' }),
  ).toContainText('Нужно решение')
  await expect(warning).toHaveCSS('font-size', '13px')
  await expect(
    warning.getByRole('button', { name: 'Предоставить решение' }),
  ).toHaveCSS('font-size', '12px')
  await warning.getByRole('button', { name: 'Предоставить решение' }).click()
  const resolution = page.getByRole('form', { name: 'Решение ожидания' })
  await resolution
    .getByLabel('Действие после сверки')
    .selectOption('continue_session')
  await expect(resolution).toContainText('с историей сообщений и инструментов')
  await expect(resolution.getByRole('textbox')).toHaveCount(0)
  await resolution
    .getByLabel('Действие после сверки')
    .selectOption('next_candidate')
  await expect(resolution).toContainText('Следующая модель: backup')
  await resolution
    .getByLabel('Действие после сверки')
    .selectOption('continue_session')
  await resolution.getByRole('button', { name: 'Сохранить решение' }).click()
  await expect(resolution).toHaveCount(0)
  const recovered = await api(page, 'GET', `/runs/${run.id}`)
  expect(recovered.state).toBe('waiting_input')
  expect(recovered.runtime.next_candidate_index).toBe(0)
  expect(
    recovered.runtime.agent_continuation.handoff_context.last_output,
  ).toContain('Reviewing the result')
  await expect(
    warning.getByRole('button', { name: 'Продолжить выполнение' }),
  ).toBeEnabled()
  await expect(warning).toContainText('Решение сохранено')
  await expect(progress.locator('.chat-run-header')).toContainText(
    'Готов к продолжению',
  )
  await page.reload()
  await page.getByRole('option', { name: new RegExp(project.name) }).click()
  await expect(warning).toContainText('Решение сохранено')
  const continueResponse = page.waitForResponse(
    (response) =>
      response.url().endsWith(`/runs/${run.id}/commands`) &&
      response.request().method() === 'POST',
  )
  await warning.getByRole('button', { name: 'Продолжить выполнение' }).click()
  const continued = await continueResponse
  expect(continued.status()).toBe(200)
  expect(continued.request().postDataJSON()).toMatchObject({
    command_type: 'resume',
  })
  await expect(warning).toHaveCount(0)
  await expect(projectAttention).toHaveCount(0)
  await expect(chatAttention).toHaveCount(0)
  await expect(projectActivity).toBeVisible()
  await expect(activity).toBeVisible()
  await expect(progress).toContainText('В очереди исполнителя')
  checkpoint(run.id, 'resume-unavailable')
  await expect(warning).toContainText(
    'Не удалось продолжить сохранённую сессию',
  )
  await expect(warning).not.toContainText('Решение сохранено')
  await expect(progress.locator('.chat-run-header')).not.toContainText(
    'Готов к продолжению',
  )
  await warning.getByRole('button', { name: 'Предоставить решение' }).click()
  await resolution
    .getByLabel('Действие после сверки')
    .selectOption('next_candidate')
  await expect(resolution).toContainText('Следующая модель: backup')
  await resolution.getByRole('button', { name: 'Сохранить решение' }).click()
  await expect(resolution).toHaveCount(0)
  await expect(warning).toContainText('Решение сохранено')
  const switched = await api(page, 'GET', `/runs/${run.id}`)
  expect(switched.runtime.next_candidate_index).toBe(1)
  expect(switched.runtime.agent_handoff.last_output).toContain(
    'Reviewing the result',
  )
  await warning.getByRole('button', { name: 'Продолжить выполнение' }).click()
  await expect(warning).toHaveCount(0)
  await expect(progress).toContainText('В очереди исполнителя')
  await page.screenshot({
    path: '../.local/chat-progress-restart.png',
    fullPage: true,
  })
  await navigation
    .getByRole('button', { name: /Review/ })
    .click({ button: 'right' })
  const stageRestart = page.getByRole('menuitem', {
    name: 'Перезапустить этап',
    exact: true,
  })
  await expect(stageRestart).toBeEnabled()
  await page.keyboard.press('Escape')
  await expect(stageRestart).toHaveCount(0)
  const reviewStage = navigation.getByRole('button', { name: /Review/ })
  await expect(reviewStage).toBeFocused()
  await reviewStage.press('Shift+F10')
  const stageRestartResponse = page.waitForResponse(
    (response) =>
      response.url().endsWith(`/runs/${run.id}/commands`) &&
      response.request().method() === 'POST',
  )
  await stageRestart.click()
  const restartedStage = await stageRestartResponse
  expect(restartedStage.status()).toBe(200)
  expect(restartedStage.request().postDataJSON()).toMatchObject({
    command_type: 'restart_stage',
    payload: { node_id: 'second' },
  })
  await expect(progress).toContainText('В очереди исполнителя')
  await expect(warning).toHaveCount(0)
  await expect(progress.getByRole('log')).toContainText('Reviewing the result')
  const sameRun = await api(page, 'GET', `/runs/${run.id}/snapshot`)
  expect(sameRun.run.id).toBe(run.id)
  expect(
    sameRun.observation.nodes.find(
      (node: { id: string }) => node.id === 'first',
    ).status,
  ).toBe('succeeded')
  expect(
    sameRun.observation.nodes.find(
      (node: { id: string }) => node.id === 'second',
    ).status,
  ).toBe('pending')
  await navigation
    .getByRole('button', { name: /end/i })
    .click({ button: 'right' })
  await expect(stageRestart).toBeDisabled()
  await page.keyboard.press('Escape')
  const restartResponse = page.waitForResponse(
    (response) =>
      response.url().endsWith(`/runs/${run.id}/restart`) &&
      response.request().method() === 'POST',
  )
  const restartIcon = progress.getByRole('button', {
    name: 'Перезапустить с первого этапа',
  })
  await expect(restartIcon.locator('svg')).toBeVisible()
  await restartIcon.click()
  const restarted = await restartResponse
  expect(restarted.status()).toBe(201)
  const fresh = await restarted.json()
  expect(fresh.id).not.toBe(run.id)
  await expect(progress).toContainText('В очереди исполнителя')
  await expect(progress).not.toContainText('Please also check README')
  const snapshot = await api(page, 'GET', `/runs/${fresh.id}/snapshot`)
  expect(
    snapshot.observation.nodes.every(
      (node: { status: string }) => node.status === 'pending',
    ),
  ).toBeTruthy()
  // No worker runs in this fixture: cancellation of the queued source is journaled.
  expect((await api(page, 'GET', `/runs/${run.id}`)).state).toBe('queued')
  expect(
    (await api(page, 'GET', `/runs/${run.id}/commands`)).at(-1).status,
  ).toBe('accepted')
})
