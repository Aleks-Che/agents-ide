import { randomUUID } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import path from 'node:path'
import { test, expect, pair, api, workspace } from './support'
import type { Page } from '@playwright/test'

const uid = () => randomUUID().slice(0, 8)
async function openNew(page: Page) {
  await pair(page)
  await page.setViewportSize({ width: 1560, height: 1000 })
  await page.getByRole('button', { name: 'Шаблоны', exact: true }).click()
  await page.getByRole('button', { name: 'Новый шаблон', exact: true }).click()
  const name = `Graph ${uid()}`
  await page.getByLabel('Название шаблона', { exact: true }).fill(name)
  await page
    .getByRole('button', { name: 'Создать шаблон', exact: true })
    .click()
  await expect(page.getByRole('heading', { name, exact: true })).toBeVisible()
  return name
}
async function add(page: Page, type: string, prompt?: string) {
  await page.getByRole('button', { name: `+ ${type}`, exact: true }).click()
  if (prompt) await page.getByLabel('Промпт', { exact: true }).fill(prompt)
}
async function connect(
  page: Page,
  source: string,
  target: string,
  when?: string,
) {
  await page
    .getByRole('button', { name: 'Добавить связь', exact: true })
    .click()
  await page
    .getByRole('combobox', { name: 'Из узла', exact: true })
    .selectOption(source)
  await page
    .getByRole('combobox', { name: 'В узел', exact: true })
    .selectOption(target)
  if (when)
    await page
      .getByRole('combobox', { name: 'Выход условия', exact: true })
      .selectOption(when)
  await page.getByRole('button', { name: 'Создать связь', exact: true }).click()
}
async function templateByName(page: Page, name: string) {
  const templates = await api(page, 'GET', '/templates')
  return templates.find((t: { name: string }) => t.name === name)
}
async function reopen(page: Page, name: string) {
  await page
    .locator('li.profile-item')
    .filter({ has: page.getByText(name, { exact: true }) })
    .click({ button: 'right' })
  await page
    .getByRole('menuitem', { name: 'Редактировать', exact: true })
    .click()
  await expect(page.getByRole('heading', { name, exact: true })).toBeVisible()
}

test('builds a repair cycle using forms, persists layout, publishes and runs its immutable version', async ({
  page,
}) => {
  test.setTimeout(90_000)
  const name = await openNew(page)
  const profile = await api(page, 'POST', '/harness_profiles', {
    name: `Graph profile ${uid()}`,
    harness_kind: 'codex',
  })
  const connection = await api(page, 'POST', '/connections', {
    name: `Graph connection ${uid()}`,
    base_url: 'http://127.0.0.1:9/v1',
    manual_models: ['test-model'],
  })
  const agentGroup = await api(page, 'POST', '/model_groups/agent', {
    name: `Agent only ${uid()}`,
    members: [{ harness_profile_id: profile.id, model_id: 'test-model' }],
  })
  const llmGroup = await api(page, 'POST', '/model_groups/llm', {
    name: `LLM only ${uid()}`,
    members: [
      { provider_connection_id: connection.id, model_id: 'test-model' },
    ],
  })
  await page.getByRole('button', { name: 'Закрыть конструктор' }).click()
  await reopen(page, name)
  await add(page, 'Start')
  await add(page, 'AgentTask', 'Implement the change')
  await page.getByLabel('Свои настройки OpenCode').check()
  await expect(page.getByLabel('Режим доступа')).toHaveValue('native')
  await page.getByLabel('Автоматически одобрять действия').check()
  await page
    .getByRole('combobox', { name: 'Выбор модели', exact: true })
    .selectOption('direct')
  await page
    .getByRole('combobox', { name: 'Профиль harness', exact: true })
    .selectOption(profile.id)
  await page.getByLabel('ID модели', { exact: true }).fill('test-model')
  await add(page, 'LLMRequest', 'Verify the change')
  await page.getByLabel('Задать: Формат ответа', { exact: true }).check()
  await page
    .getByRole('combobox', { name: 'Формат ответа', exact: true })
    .selectOption({ label: 'json' })
  await page.getByLabel('Удалять блоки размышлений перед JSON').check()
  await page
    .getByLabel('Извлекать JSON из Markdown и окружающего текста')
    .check()
  await page
    .getByRole('combobox', { name: 'Выбор модели', exact: true })
    .selectOption('group')
  const groupSelect = page.getByRole('combobox', {
    name: 'Группа моделей',
    exact: true,
  })
  await expect(
    groupSelect.locator(`option[value="${agentGroup.id}"]`),
  ).toHaveCount(0)
  await groupSelect.selectOption(llmGroup.id)
  await expect(
    page.getByRole('list', { name: 'Приоритет кандидатов' }),
  ).toContainText('test-model')
  await page
    .getByRole('combobox', { name: 'Выбор модели', exact: true })
    .selectOption('direct')
  await page
    .getByRole('combobox', { name: 'LLM-подключение', exact: true })
    .selectOption(connection.id)
  await page.getByLabel('ID модели', { exact: true }).fill('test-model')
  await add(page, 'Condition')
  await page
    .getByRole('combobox', { name: 'Оператор', exact: true })
    .selectOption('ref')
  await page
    .getByLabel('Переменная', { exact: true })
    .fill('steps.llmrequest_1.latest.decision')
  await add(page, 'End')
  await connect(page, 'start_1', 'agenttask_1')
  await connect(page, 'agenttask_1', 'llmrequest_1')
  await connect(page, 'llmrequest_1', 'condition_1')
  await connect(page, 'condition_1', 'end_1', 'true')
  await connect(page, 'condition_1', 'end_1', 'unknown')
  await connect(page, 'condition_1', 'agenttask_1', 'false')
  await page.getByLabel('Задать: loop', { exact: true }).check()
  await page.getByLabel('id', { exact: true }).fill('repair')
  await page.getByLabel('max_iterations', { exact: true }).fill('3')
  await page.getByLabel('work.mode', { exact: true }).check()
  await page
    .getByRole('combobox', { name: 'Режим после перехода', exact: true })
    .selectOption('repair')
  await page
    .getByRole('button', { name: 'Проверить граф', exact: true })
    .click()
  await expect(page.getByRole('dialog').getByRole('status')).toContainText(
    'Серверная проверка графа пройдена',
  )
  // Ordinary nodes cannot fan out even through the accessible edge form.
  await page
    .getByRole('button', { name: 'Добавить связь', exact: true })
    .click()
  await page
    .getByRole('combobox', { name: 'Из узла', exact: true })
    .selectOption('agenttask_1')
  await page
    .getByRole('combobox', { name: 'В узел', exact: true })
    .selectOption('end_1')
  await expect(
    page.getByRole('button', { name: 'Создать связь', exact: true }),
  ).toBeDisabled()
  await expect(
    page.getByText('Параллельные выходы запрещены.', { exact: false }),
  ).toBeVisible()
  await page.getByRole('button', { name: 'Сохранить', exact: true }).click()
  await expect(page.getByRole('dialog').getByRole('status')).toHaveText(
    'Шаблон сохранён. Существующие привязки обновлены.',
  )
  let template = await templateByName(page, name)
  expect(template.draft.graph.edges).toHaveLength(6)
  expect(
    template.draft.graph.nodes.find(
      (node: { id: string }) => node.id === 'agenttask_1',
    ).config.harness_settings,
  ).toEqual({ opencode: { permission_mode: 'native', auto_approve: true } })
  expect(template.draft.graph.edges.at(-1)).toMatchObject({
    when: 'false',
    loop: { id: 'repair', max_iterations: 3 },
    assignments: { 'work.mode': { const: 'repair' } },
  })
  await page.getByRole('button', { name: 'Сохранить', exact: true }).click()
  await expect(page.getByRole('dialog').getByRole('status')).toContainText(
    'Шаблон сохранён',
  )
  const firstVersion = (
    await api(page, 'GET', `/templates/${template.id}/versions`)
  )[0]
  expect(
    firstVersion.graph.nodes.find(
      (item: { id: string }) => item.id === 'llmrequest_1',
    ).config.json_processing,
  ).toEqual({ strip_thinking_tags: true, extract_json: true })
  const node = page
    .locator('.react-flow__node')
    .filter({ hasText: 'agenttask_1' })
  const box = (await node.boundingBox())!
  await page.mouse.move(box.x + 40, box.y + 35)
  await page.mouse.down()
  await page.mouse.move(box.x + 85, box.y + 85, { steps: 8 })
  await page.mouse.up()
  await page.getByRole('button', { name: 'Сохранить', exact: true }).click()
  await expect(page.getByRole('dialog').getByRole('status')).toContainText(
    'Шаблон сохранён',
  )
  await page.getByRole('button', { name: 'Сохранить', exact: true }).click()
  await expect(page.getByRole('dialog').getByRole('status')).toContainText(
    'Шаблон сохранён',
  )
  const secondVersion = (
    await api(page, 'GET', `/templates/${template.id}/versions`)
  )[0]
  expect(secondVersion.execution_hash).toBe(firstVersion.execution_hash)
  template = await templateByName(page, name)
  const position = template.draft.graph.nodes.find(
    (n: { id: string }) => n.id === 'agenttask_1',
  ).position
  await page.getByRole('button', { name: 'Fit View', exact: true }).click()
  await expect
    .poll(async () => {
      const canvas = (await page.locator('.graph-canvas').boundingBox())!
      const boxes = await page
        .locator('.react-flow__node')
        .evaluateAll((nodes) =>
          nodes.map((node) => {
            const box = node.getBoundingClientRect()
            return { x: box.x, y: box.y, width: box.width, height: box.height }
          }),
        )
      return (
        boxes.length === 5 &&
        boxes.every(
          (box) =>
            box.width > 50 &&
            box.x >= canvas.x &&
            box.y >= canvas.y &&
            box.x + box.width <= canvas.x + canvas.width &&
            box.y + box.height <= canvas.y + canvas.height,
        )
      )
    })
    .toBe(true)
  await page.screenshot({ path: '../.local/stage10-graph-editor.png' })
  await page.getByRole('button', { name: 'Закрыть конструктор' }).click()
  await reopen(page, name)
  expect(
    (await templateByName(page, name)).draft.graph.nodes.find(
      (n: { id: string }) => n.id === 'agenttask_1',
    ).position,
  ).toEqual(position)
  await page.getByRole('button', { name: 'Закрыть конструктор' }).click()
  const project = await api(page, 'POST', '/projects', {
    name: `Project ${uid()}`,
    workspace_path: workspace(),
  })
  const chat = await api(page, 'POST', `/projects/${project.id}/chats`, {
    title: `Chat ${uid()}`,
  })
  const binding = await api(
    page,
    'POST',
    `/versions/${secondVersion.id}/bindings`,
    { project_id: project.id, name: `Binding ${uid()}` },
  )
  await page.reload()
  await page.getByRole('option', { name: new RegExp(project.name) }).click()
  await page.getByRole('option', { name: new RegExp(chat.title) }).click()
  await page.getByRole('button', { name: 'Запустить шаблон' }).click()
  await page
    .getByRole('combobox', { name: 'Шаблон проекта', exact: true })
    .selectOption(binding.id)
  await page.getByLabel('Имитация (fake)').check()
  await page.getByRole('button', { name: 'Запустить preflight' }).click()
  await expect(
    page.getByRole('button', { name: 'Запустить', exact: true }),
  ).toBeEnabled()
  await page.getByRole('button', { name: 'Запустить', exact: true }).click()
  await page.getByRole('button', { name: 'Подробности', exact: true }).click()
  await expect(page.getByRole('heading', { name: /Run ·/ })).toBeVisible()
  const run = (await api(page, 'GET', `/runs?chat_id=${chat.id}`))[0]
  const result = execFileSync(
    path.resolve(
      '../backend/.venv',
      process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
    ),
    [
      'tests/e2e/execute_selection_run.py',
      process.env.AGENTS_IDE_E2E_DATA_DIR!,
      run.id,
      'execute',
    ],
    { encoding: 'utf8', windowsHide: true },
  )
  expect(result.trim()).toBe('completed')
})

test('shows validation at a node and retains local edits when another editor saved', async ({
  page,
}) => {
  const name = await openNew(page)
  await add(page, 'Start')
  await add(page, 'End')
  await connect(page, 'start_1', 'end_1')
  await add(page, 'LLMRequest')
  await page.getByRole('button', { name: 'Проверить граф' }).click()
  const issues = page.getByRole('list', { name: 'Результаты проверки' })
  await expect(issues).toContainText('llmrequest_1')
  await issues
    .getByRole('button')
    .filter({ hasText: 'llmrequest_1' })
    .first()
    .click()
  await expect(page.getByLabel('Промпт', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Удалить узел' }).click()
  await page.getByRole('button', { name: 'Сохранить' }).click()
  await expect(page.getByRole('dialog').getByRole('status')).toContainText(
    'Шаблон сохранён',
  )
  const template = await templateByName(page, name)
  await page.getByRole('button', { name: 'start_1', exact: true }).click()
  await page.getByLabel('Задать: Подпись', { exact: true }).first().check()
  await page.getByLabel('Подпись', { exact: true }).first().fill('Local label')
  await api(page, 'PUT', `/templates/${template.id}/draft`, {
    ...template.draft,
    expected_version: template.version,
    inputs: { external: 'saved' },
  })
  await page.getByRole('button', { name: 'Сохранить' }).click()
  await expect(page.getByRole('alert')).toContainText(
    'Черновик изменён в другом месте',
  )
  await expect(page.getByLabel('Подпись', { exact: true }).first()).toHaveValue(
    'Local label',
  )
  await page.getByRole('button', { name: 'Закрыть конструктор' }).click()
  await expect(
    page.getByText('Есть несохранённые правки.', { exact: false }).last(),
  ).toBeVisible()
  await page.getByRole('button', { name: 'Продолжить редактирование' }).click()
  await page
    .getByRole('button', { name: 'Загрузить сохранённый шаблон' })
    .click()
  await page
    .getByRole('button', { name: 'Заменить мои правки', exact: true })
    .click()
  await expect(page.getByRole('alert')).toHaveCount(0)
  await expect(
    page
      .getByRole('dialog')
      .getByText('Шаблон сохранён', { exact: false })
      .first(),
  ).toBeVisible()
})

test('imports graph with explicit resource mapping, reviews backup candidates and preserves provenance', async ({
  page,
}) => {
  const name = await openNew(page)
  const connection = await api(page, 'POST', '/connections', {
    name: `Local ${uid()}`,
    base_url: 'http://127.0.0.1:9/v1',
    manual_models: ['first', 'backup'],
  })
  // Reopening reloads the local catalog after resources were created.
  await page.getByRole('button', { name: 'Закрыть конструктор' }).click()
  await reopen(page, name)
  await page.getByRole('button', { name: 'Импорт', exact: true }).click()
  await page.getByText('Перенос групп моделей', { exact: true }).click()
  await page.getByLabel('Файл группы', { exact: true }).setInputFiles({
    name: 'group.json',
    mimeType: 'application/json',
    buffer: Buffer.from(
      JSON.stringify({
        schema_version: '1.0.0',
        kind: 'llm',
        name: `Imported ${uid()}`,
        members: [
          {
            resource_ref: 'foreign',
            model_id: 'first',
            enabled: false,
            params: {},
          },
          {
            resource_ref: 'foreign',
            model_id: 'backup',
            enabled: true,
            params: {},
          },
        ],
      }),
    ),
  })
  await page
    .getByRole('combobox', { name: 'Ресурс: foreign', exact: true })
    .selectOption(connection.id)
  await expect(
    page.getByRole('button', { name: 'Импортировать группу' }),
  ).toBeDisabled()
  await expect(page.getByText('2. backup', { exact: false })).toBeVisible()
  await page.getByLabel('Проверены назначения всех кандидатов группы').check()
  await page.getByRole('button', { name: 'Импортировать группу' }).click()
  await expect(page.getByLabel('Ресурс: foreign')).toHaveCount(0)
  const group = (await api(page, 'GET', '/model_groups')).find(
    (g: { name: string }) => g.name.startsWith('Imported'),
  )
  const document = {
    schema_version: '1.0.0',
    trusted: true,
    execution_hash: '0'.repeat(64),
    graph: {
      nodes: [
        { id: 'start', type: 'Start' },
        {
          id: 'review',
          type: 'LLMRequest',
          config: {
            prompt: 'review',
            model_selection: { kind: 'group', group_id: 'foreign-group' },
          },
        },
        { id: 'end', type: 'End' },
      ],
      edges: [
        { from: 'start', to: 'review' },
        { from: 'review', to: 'end' },
      ],
    },
  }
  await page.getByLabel('Файл графа', { exact: true }).setInputFiles({
    name: 'graph.json',
    mimeType: 'application/json',
    buffer: Buffer.from(JSON.stringify(document)),
  })
  await page
    .getByRole('combobox', { name: 'group / llm: foreign-group', exact: true })
    .selectOption(group.id)
  await expect(
    page.getByRole('button', { name: 'Применить импорт' }),
  ).toBeDisabled()
  await page
    .getByLabel(
      'Я проверил программы, аргументы, роли, пути и назначения всех кандидатов',
    )
    .check()
  await page.getByRole('button', { name: 'Применить импорт' }).click()
  await expect(
    page.getByRole('heading', { name: 'Входы, роли и лимиты' }),
  ).toBeVisible()
  await page.getByRole('button', { name: 'Сохранить' }).click()
  await expect(page.getByRole('dialog').getByRole('status')).toContainText(
    'Шаблон сохранён',
  )
  const saved = await templateByName(page, name)
  expect(saved.draft.origin).toBe('imported')
  expect(
    saved.draft.graph.edges.every(
      (edge: { id?: string }) => edge.id === undefined,
    ),
  ).toBe(true)
  expect(saved.draft.graph.nodes[1].config.model_selection.group_id).toBe(
    group.id,
  )
  expect(saved.draft.trusted).toBeUndefined()
  await page.getByRole('button', { name: 'Сохранить' }).click()
  await expect(page.getByRole('dialog').getByRole('status')).toContainText(
    'Шаблон сохранён',
  )
  const version = (await api(page, 'GET', `/templates/${saved.id}/versions`))[0]
  expect(version.origin).toBe('imported')
  expect(version.execution_hash).not.toBe(document.execution_hash)
})

test('edits Command lists, CollectContext sources and PlanControl using server schema fields', async ({
  page,
}) => {
  const name = await openNew(page)
  await add(page, 'Start')
  await add(page, 'Command')
  const command = page.getByRole('group', { name: 'Команды 1', exact: true })
  await command.getByLabel('id', { exact: true }).fill('check')
  await command.getByLabel('Программа', { exact: true }).fill('python')
  await command
    .getByRole('button', { name: 'Добавить: Аргументы', exact: true })
    .click()
  await command.getByLabel('Аргументы 1', { exact: true }).fill('--version')
  await add(page, 'CollectContext')
  await page.getByLabel('Задать: Источники', { exact: true }).check()
  await page
    .getByRole('button', { name: 'Добавить: Источники', exact: true })
    .click()
  const source = page.getByRole('group', { name: 'Источники 1', exact: true })
  await source
    .getByRole('combobox', { name: 'Вид', exact: true })
    .selectOption('file')
  await source.getByLabel('Задать: Путь', { exact: true }).check()
  await source.getByLabel('Путь', { exact: true }).fill('README.md')
  await add(page, 'PlanControl')
  await page
    .getByRole('combobox', { name: 'Операция', exact: true })
    .selectOption('select_next')
  await page.getByLabel('Задать: Условие завершения', { exact: true }).check()
  await page
    .getByRole('combobox', { name: 'Условие завершения', exact: true })
    .selectOption('verified_only')
  await add(page, 'End')
  await connect(page, 'start_1', 'command_1')
  await connect(page, 'command_1', 'collectcontext_1')
  await connect(page, 'collectcontext_1', 'plancontrol_1')
  await connect(page, 'plancontrol_1', 'end_1')
  await page.getByRole('button', { name: 'Входы и роли', exact: true }).click()
  const inputSchema = page.getByRole('group', {
    name: 'Схема входов',
    exact: true,
  })
  await inputSchema
    .getByLabel('Имя нового поля: Схема входов', { exact: true })
    .fill('task')
  await inputSchema
    .getByRole('button', { name: 'Добавить поле схемы', exact: true })
    .click()
  await inputSchema
    .getByLabel('Обязательное поле: task', { exact: true })
    .check()
  const defaults = page.getByRole('group', {
    name: 'Начальные значения входов',
    exact: true,
  })
  await defaults.getByLabel('task', { exact: true }).fill('Check the project')
  await page.getByRole('button', { name: 'Сохранить' }).click()
  await expect(page.getByRole('dialog').getByRole('status')).toContainText(
    'Шаблон сохранён',
  )
  const saved = await templateByName(page, name)
  expect(saved.draft.inputs.task).toBe('Check the project')
  expect(saved.draft.graph.nodes[1].config.commands).toEqual([
    {
      id: 'check',
      program: 'python',
      args: ['--version'],
      success_exit_codes: [0],
    },
  ])
  expect(saved.draft.graph.nodes[2].config.sources).toEqual([
    { kind: 'file', path: 'README.md' },
  ])
  expect(saved.draft.graph.nodes[3].config).toEqual({
    operation: 'select_next',
    completion_policy: 'verified_only',
  })
  await page.getByRole('button', { name: 'Сохранить' }).click()
  await expect(page.getByRole('dialog').getByRole('status')).toContainText(
    'Шаблон сохранён',
  )
})

test('opens a preset copy and changes reviewer from agent to LLM without altering the original', async ({
  page,
}) => {
  await pair(page)
  await page.setViewportSize({ width: 1560, height: 1000 })
  const presets = await api(page, 'GET', '/presets')
  const original = (
    await api(page, 'GET', `/templates/${presets[0].template_id}/versions`)
  )[0]
  await page.getByRole('button', { name: 'Шаблоны', exact: true }).click()
  await page
    .locator('.profile-item')
    .filter({ has: page.getByText('встроенный', { exact: true }) })
    .first()
    .click({ button: 'right' })
  await page.getByRole('menuitem', { name: 'Копировать', exact: true }).click()
  const name = `Editable preset ${uid()}`
  await page.getByLabel('Название шаблона', { exact: true }).fill(name)
  await page
    .getByRole('dialog')
    .getByRole('button', { name: 'Копировать', exact: true })
    .click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await reopen(page, name)
  await page.getByRole('button', { name: 'reviewer', exact: true }).click()
  await page
    .getByRole('combobox', { name: 'Тип исполнителя', exact: true })
    .selectOption('LLMRequest')
  await page.getByRole('button', { name: 'Сохранить' }).click()
  await expect(page.getByRole('dialog').getByRole('status')).toContainText(
    'Шаблон сохранён',
  )
  await page.getByRole('button', { name: 'Сохранить' }).click()
  await expect(page.getByRole('dialog').getByRole('status')).toContainText(
    'Шаблон сохранён',
  )
  const template = await templateByName(page, name)
  const version = (
    await api(page, 'GET', `/templates/${template.id}/versions`)
  )[0]
  expect(
    version.graph.nodes.find((n: { id: string }) => n.id === 'reviewer').type,
  ).toBe('LLMRequest')
  expect(version.graph.roles.reviewer).toBe('llm')
  expect((await api(page, 'GET', `/versions/${original.id}`)).graph).toEqual(
    original.graph,
  )
})
