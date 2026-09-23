import { execFileSync } from 'node:child_process'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { randomUUID } from 'node:crypto'
import { test, expect, pair, api, workspace } from './support'

for (const mode of ['manual', 'assistant'] as const) {
  test(`AI assistance accepts HEAD before AgentTask and keeps local work (${mode})`, async ({
    page,
  }) => {
    await pair(page)
    const root = workspace()
    mkdirSync(path.join(root, 'src/__pycache__'), { recursive: true })
    writeFileSync(
      path.join(root, 'src/__pycache__/cache.pyc'),
      'generated cache',
    )
    const git = (...args: string[]) =>
      execFileSync('git', ['-C', root, ...args], { windowsHide: true })
        .toString()
        .trim()
    git('add', '.')
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
      name: `HEAD ${randomUUID()}`,
      workspace_path: root,
    })
    const chat = await api(page, 'POST', `/projects/${project.id}/chats`, {
      title: 'wiki-doc-skill',
    })
    const template = await api(page, 'POST', '/templates', {
      name: `HEAD ${randomUUID()}`,
    })
    const profile = await api(page, 'POST', '/harness_profiles', {
      name: `head-test-${mode}-${randomUUID()}`,
      harness_kind: 'codex',
      settings: { permission_mode: 'read_only' },
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
              id: 'agent',
              type: 'AgentTask',
              config: {
                prompt: 'continue',
                harness_profile_id: profile.id,
                model: 'test',
              },
            },
            {
              id: 'commit',
              type: 'GitCommit',
              config: { allowlist: ['src/**'], generate_message: false },
            },
            { id: 'end', type: 'End' },
          ],
          edges: [
            { from: 'start', to: 'agent' },
            { from: 'agent', to: 'commit' },
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
      message: 'continue',
      idempotency_key: randomUUID(),
    })
    execFileSync(
      path.resolve(
        '../backend/.venv',
        process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
      ),
      [
        path.resolve('tests/e2e/seed_git_head_changes.py'),
        process.env.AGENTS_IDE_E2E_DATA_DIR!,
        run.id,
      ],
      { windowsHide: true },
    )
    const head = git('rev-parse', 'HEAD')
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
    await expect(
      assistant.getByText(
        'Выберите модель, чтобы сгенерировать дополнительные вопросы.',
      ),
    ).toBeVisible()
    const primary = assistant.getByRole('button', {
      name: /^Объясни и предложи решение по существующей проблеме/,
    })
    await expect(primary).toBeVisible()
    await expect(primary).toContainText(
      'А конкретнее: этап agent, external_change_detected.',
    )
    await primary.click()
    await expect(assistant.getByLabel('Вопрос помощнику')).toHaveValue(
      new RegExp(`Область проекта: ${project.name}`),
    )
    await assistant
      .getByRole('button', {
        name:
          mode === 'assistant'
            ? 'Решить через помощника'
            : 'Сравнить и принять изменения',
      })
      .click()
    const form = page.getByRole('form', { name: 'Принятие изменений Git' })
    await expect(
      form.getByText('Новые коммиты поверх ожидаемого HEAD'),
    ).toBeVisible()
    await expect(
      form.getByText('src/draft.txt', { exact: false }),
    ).toBeVisible()
    await expect(
      form.getByText('remove generated cache', { exact: false }),
    ).toBeVisible()
    await form.screenshot({
      path: path.resolve('../.local/git-head-review.png'),
    })
    if (mode === 'assistant') {
      await expect(
        assistant.getByRole('form', { name: 'Принятие изменений Git' }),
      ).toBeVisible()
      const unchanged = await api(page, 'GET', `/runs/${run.id}`)
      expect(unchanged.state).toBe('waiting_input')
      expect(
        unchanged.waiting_reason.resolution_schema.git_changes.accepted,
      ).toBe(false)
    }
    const accept = form.getByRole('button', {
      name: mode === 'assistant' ? 'Принять HEAD и продолжить' : 'Принять HEAD',
      exact: true,
    })
    await expect(accept).toBeDisabled()
    await form
      .getByRole('checkbox', {
        name: 'Принять текущий HEAD как основу следующего этапа',
      })
      .check()
    await expect(accept).toBeDisabled()
    await form.getByRole('checkbox', { name: /Я изучил сравнение/ }).check()
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
          /Инструмент помощника принял проверенное изменение HEAD/,
        ),
      ).toBeVisible()
      await expect(
        assistant.getByRole('button', { name: 'Решить через помощника' }),
      ).toHaveCount(0)
    }
    expect(git('rev-parse', 'HEAD')).toBe(head)
    expect(readFileSync(path.join(root, 'src/draft.txt'), 'utf8')).toBe(
      'unfinished work\n',
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
