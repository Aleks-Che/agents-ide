import { execFileSync } from 'node:child_process'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { randomUUID } from 'node:crypto'
import { test, expect, pair, api, workspace } from './support'

for (const mode of [
  'manual',
  'assistant',
  'assistant-before-dispatch',
  'ignored-before-dispatch',
] as const) {
  test(`AI diagnosis accepts protected files with stale checks (${mode})`, async ({
    page,
  }) => {
    await pair(page)
    const root = workspace()
    mkdirSync(path.join(root, 'src'))
    mkdirSync(path.join(root, 'frontend'))
    writeFileSync(path.join(root, 'src/a.txt'), 'base\n')
    writeFileSync(
      path.join(root, '.gitignore'),
      mode === 'ignored-before-dispatch' ? '*.log\n' : '*.cache\n',
    )
    writeFileSync(
      path.join(root, 'frontend/review-check.log'),
      'old log content\n',
    )
    const git = (...args: string[]) =>
      execFileSync('git', ['-C', root, ...args], { windowsHide: true })
    git('add', 'src/a.txt', '.gitignore')
    git(
      '-c',
      'user.name=Test',
      '-c',
      'user.email=test@example.invalid',
      'commit',
      '-qm',
      'initial',
    )
    const project = await api(page, 'POST', '/projects', {
      name: `Accept ${randomUUID()}`,
      workspace_path: root,
    })
    const chat = await api(page, 'POST', `/projects/${project.id}/chats`, {
      title: 'Фронтенд',
    })
    const template = await api(page, 'POST', '/templates', {
      name: `Git ${randomUUID()}`,
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
              config: { allowlist: ['src/**'], generate_message: false },
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
        path.resolve('tests/e2e/seed_git_changes.py'),
        process.env.AGENTS_IDE_E2E_DATA_DIR!,
        run.id,
        ...(mode === 'assistant-before-dispatch' ? ['before-dispatch'] : []),
        ...(mode === 'ignored-before-dispatch'
          ? ['ignored-before-dispatch']
          : []),
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
      .getByRole('button', {
        name:
          mode !== 'manual'
            ? 'Решить через помощника'
            : 'Сравнить и принять изменения',
      })
      .click()
    const form = page.getByRole('form', { name: 'Принятие изменений Git' })
    if (mode === 'ignored-before-dispatch') {
      const originalContents = readFileSync(
        path.join(root, 'frontend/review-check.log'),
      )
      await expect(form.getByText(/Принимать файлы не требуется/)).toBeVisible()
      await expect(form.getByRole('checkbox')).toHaveCount(0)
      await expect(
        form.getByRole('button', {
          name: 'Принять выбранные изменения и продолжить',
        }),
      ).toHaveCount(0)
      await form.getByRole('button', { name: 'Закрыть', exact: true }).click()
      await assistant
        .getByRole('button', { name: 'Свернуть помощника' })
        .click()
      await page
        .getByRole('region', { name: 'Выполнение шаблона', exact: true })
        .getByRole('button', { name: 'Продолжить выполнение', exact: true })
        .click()
      await expect
        .poll(async () => (await api(page, 'GET', `/runs/${run.id}`)).state)
        .toBe('queued')
      expect(
        readFileSync(path.join(root, 'frontend/review-check.log')),
      ).toEqual(originalContents)
      return
    }
    if (mode !== 'manual') {
      await expect(
        assistant.getByRole('form', { name: 'Принятие изменений Git' }),
      ).toBeVisible()
      await expect(
        assistant.getByRole('button', { name: 'Свернуть помощника' }),
      ).toBeInViewport({ ratio: 1 })
      await expect(assistant).toHaveJSProperty('scrollTop', 0)
      await expect(
        assistant.getByText(/Ожидается ваше подтверждение/),
      ).toBeVisible()
      await page.setViewportSize({ width: 390, height: 844 })
      await assistant.getByRole('button', { name: 'Развернуть чат' }).click()
      await assistant.getByRole('button', { name: 'Уменьшить чат' }).click()
      await expect(form.getByText(/Не выбрано: 1/)).toBeVisible()
      const unchanged = await api(page, 'GET', `/runs/${run.id}`)
      expect(
        unchanged.waiting_reason.resolution_schema.git_changes.accepted,
      ).toBe(false)
      if (mode === 'assistant-before-dispatch') {
        expect(
          unchanged.waiting_reason.resolution_schema.git_changes.attempt_id,
        ).toBeNull()
        expect(
          unchanged.waiting_reason.resolution_schema.git_changes
            .before_dispatch,
        ).toBe(true)
      }
    }
    await expect(form.getByText('Риск:', { exact: false })).toContainText(
      'Не установлен',
    )
    await expect(
      form.getByText('Исходный текст не сохранён;', { exact: false }),
    ).toBeVisible()
    await form.screenshot({
      path: path.resolve(`../.local/git-changes-review-${mode}.png`),
    })
    const accept = form.getByRole('button', {
      name:
        mode !== 'manual'
          ? 'Принять выбранные изменения и продолжить'
          : 'Принять выбранные изменения',
      exact: true,
    })
    const acknowledged = form.getByRole('checkbox', {
      name: /Я изучил сравнение/,
    })
    await expect(accept).toBeDisabled()
    await form
      .getByRole('checkbox', { name: 'frontend/review-check.log' })
      .check()
    await expect(accept).toBeDisabled()
    await acknowledged.check()
    if (mode !== 'manual') {
      await expect(
        assistant.getByRole('button', { name: 'Свернуть помощника' }),
      ).toBeInViewport({ ratio: 1 })
      await expect(assistant).toHaveJSProperty('scrollTop', 0)
    }
    writeFileSync(
      path.join(root, 'frontend/review-check.log'),
      'changed after opening review\n',
    )
    await accept.click()
    await expect(
      page
        .getByRole('alert')
        .filter({ hasText: 'Состояние изменилось после сравнения' }),
    ).toBeVisible()
    await form.getByRole('button', { name: 'Обновить сравнение' }).click()
    await expect(acknowledged).not.toBeChecked()
    await form
      .getByRole('checkbox', { name: 'frontend/review-check.log' })
      .check()
    await acknowledged.check()
    await accept.click()
    await expect(form).toHaveCount(0)
    const saved = await api(page, 'GET', `/runs/${run.id}`)
    if (mode === 'manual') {
      expect(saved.state).toBe('waiting_input')
      expect(saved.waiting_reason.resolution_schema.git_changes.accepted).toBe(
        true,
      )
    } else {
      expect(saved.state).toBe('queued')
      await expect(
        assistant.getByText(
          /Инструмент помощника принял изменения защищённых файлов: frontend\/review-check.log/,
        ),
      ).toBeVisible()
      await expect(
        assistant.getByRole('button', { name: 'Решить через помощника' }),
      ).toHaveCount(0)
    }
    expect(
      readFileSync(path.join(root, 'frontend/review-check.log'), 'utf-8'),
    ).toBe('changed after opening review\n')
    expect(git('ls-files', 'frontend/review-check.log').toString().trim()).toBe(
      '',
    )
    if (mode === 'manual')
      await page
        .getByRole('region', { name: 'Управление Run' })
        .getByRole('button', { name: 'Продолжить', exact: true })
        .click()
    await expect
      .poll(async () => (await api(page, 'GET', `/runs/${run.id}`)).state)
      .toBe('queued')
  })
}
