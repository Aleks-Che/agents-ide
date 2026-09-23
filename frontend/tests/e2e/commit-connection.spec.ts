import { execFileSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { writeFileSync } from 'node:fs'
import path from 'node:path'
import { test, expect, pair, api, workspace } from './support'

for (const boundary of ['first-attempt', 'guard-retry']) {
  test(`assistant compares a changed commit connection and resumes after confirmation (${boundary})`, async ({
    page,
  }) => {
    await pair(page)
    const root = workspace()
    writeFileSync(path.join(root, 'README.md'), 'base\n')
    const git = (...args: string[]) =>
      execFileSync('git', ['-C', root, ...args], { windowsHide: true })
    git('add', '.')
    git(
      '-c',
      'user.name=Test',
      '-c',
      'user.email=test@example.invalid',
      'commit',
      '-qm',
      'base',
    )
    const project = await api(page, 'POST', '/projects', {
      name: `Connection ${randomUUID()}`,
      workspace_path: root,
    })
    const chat = await api(page, 'POST', `/projects/${project.id}/chats`, {
      title: 'Бэкэнд',
    })
    const connection = await api(page, 'POST', '/connections', {
      name: `Commit provider ${randomUUID()}`,
      base_url: 'http://127.0.0.1:9/v1',
    })
    const template = await api(page, 'POST', '/templates', {
      name: `Connection ${randomUUID()}`,
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
              id: 'commit',
              type: 'GitCommit',
              config: {
                allowlist: ['**'],
                generate_message: true,
                message_generation: {
                  connection_id: connection.id,
                  model: 'test',
                },
              },
            },
            { id: 'end', type: 'End' },
          ],
          edges: [
            { from: 'start', to: 'commit' },
            { from: 'commit', to: 'end' },
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
        name: 'main',
      },
    )
    const run = await api(page, 'POST', '/runs', {
      project_id: project.id,
      chat_id: chat.id,
      binding_id: binding.id,
      execution_mode: 'real',
      message: 'commit',
      idempotency_key: randomUUID(),
    })
    execFileSync(
      path.resolve(
        '../backend/.venv',
        process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
      ),
      [
        path.resolve('tests/e2e/seed_commit_connection.py'),
        process.env.AGENTS_IDE_E2E_DATA_DIR!,
        run.id,
        boundary,
      ],
      { windowsHide: true },
    )
    await page.reload()
    const projectOption = page.getByRole('option', {
      name: new RegExp(project.name),
    })
    await projectOption.click()
    await page.getByRole('button', { name: 'ИИ-помощь', exact: true }).click()
    const assistant = page.getByRole('dialog', { name: 'ИИ-помощник' })
    await assistant
      .getByRole('button', { name: 'Выбрать блок курсором' })
      .click()
    await projectOption.click()
    await assistant
      .getByRole('button', { name: 'Решить через помощника' })
      .click()
    const form = assistant.getByRole('form', {
      name: 'Обновление подключения GitCommit',
    })
    await expect(form.getByText(/Ожидаемая версия: 1/)).toContainText(
      'Текущая версия: 2',
    )
    await expect(
      form.getByText(/Исполняемые настройки совпадают/),
    ).toBeVisible()
    if (boundary === 'guard-retry') {
      await expect(
        form.getByText(/Предыдущая попытка завершилась/),
      ).toBeVisible()
    }
    const confirm = form.getByRole('button', {
      name: 'Обновить версию подключения и продолжить',
      exact: true,
    })
    const consent = form.getByRole('checkbox', { name: /Я изучил сравнение/ })
    await expect(confirm).toBeDisabled()
    await consent.check()
    await api(page, 'PATCH', `/connections/${connection.id}`, {
      expected_version: 2,
      name: `${connection.name} renamed`,
    })
    await confirm.click()
    await expect(assistant.getByRole('alert')).toContainText(
      'Обновите сравнение',
    )
    expect((await api(page, 'GET', `/runs/${run.id}`)).state).toBe(
      'waiting_input',
    )
    await form.getByRole('button', { name: 'Обновить сравнение' }).click()
    await expect(form.getByText(/Ожидаемая версия: 1/)).toContainText(
      'Текущая версия: 3',
    )
    await expect(consent).not.toBeChecked()
    await consent.check()
    await confirm.click()
    await expect(form).toHaveCount(0)
    await expect(
      assistant.getByText(/Инструмент помощника обновил ожидаемую версию/),
    ).toBeVisible()
    expect((await api(page, 'GET', `/runs/${run.id}`)).state).toBe('queued')
    expect(git('rev-list', '--count', 'HEAD').toString().trim()).toBe('1')
  })
}
