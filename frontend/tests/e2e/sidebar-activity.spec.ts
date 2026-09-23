import { randomUUID } from 'node:crypto'
import type { SidebarActivity } from '../../src/api/activity'
import { test, expect, pair, api, workspace } from './support'

test('project and chat indicators update while another project is selected', async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  let activity: SidebarActivity = { projects: {}, chats: {} }
  await page.route('**/api/sidebar/activity', (route) =>
    route.fulfill({ json: activity }),
  )
  await pair(page)
  const working = await api(page, 'POST', '/projects', {
    name: `Working ${randomUUID()}`,
    workspace_path: workspace(),
  })
  const other = await api(page, 'POST', '/projects', {
    name: `Other ${randomUUID()}`,
    workspace_path: workspace(),
  })
  const chat = await api(page, 'POST', `/projects/${working.id}/chats`, {
    title: 'Working chat',
  })
  const idle = await api(page, 'POST', `/projects/${working.id}/chats`, {
    title: 'Idle chat',
  })
  activity = {
    projects: { [working.id]: { running: true, attention: false } },
    chats: { [chat.id]: { running: true, attention: false } },
  }
  await page.reload()
  const workingOption = page.getByRole('option', {
    name: new RegExp(working.name),
  })
  const otherOption = page.getByRole('option', { name: new RegExp(other.name) })
  await otherOption.click()
  const projectRunning = workingOption.getByRole('status', {
    name: 'В проекте выполняется процесс',
  })
  const projectAttention = workingOption.getByRole('status', {
    name: 'В проекте требуется внимание: ошибка или запрос',
  })
  await expect(projectRunning).toBeVisible()
  await expect(projectRunning.locator('svg')).toHaveCSS(
    'animation-name',
    'chat-activity-spin',
  )
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await expect(projectRunning.locator('svg')).toHaveCSS(
    'animation-name',
    'chat-activity-spin',
  )
  await expect(projectRunning.locator('svg')).toHaveCSS('opacity', '1')
  const rotation = await projectRunning
    .locator('svg')
    .evaluate((element) => getComputedStyle(element).transform)
  await expect
    .poll(() =>
      projectRunning
        .locator('svg')
        .evaluate((element) => getComputedStyle(element).transform),
    )
    .not.toBe(rotation)
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  await expect(otherOption.getByRole('status')).toHaveCount(0)
  await expect(workingOption).toHaveAttribute('aria-selected', 'false')

  activity = {
    projects: { [working.id]: { running: true, attention: true } },
    chats: {
      [chat.id]: { running: false, attention: true },
      [idle.id]: { running: true, attention: false },
    },
  }
  await expect(projectAttention).toBeVisible()
  await expect(projectRunning).toBeVisible()
  await workingOption.click()
  const chatOption = page.getByRole('option', { name: /Working chat/ })
  await expect(
    chatOption.getByRole('status', {
      name: 'В диалоге требуется внимание: ошибка или запрос',
    }),
  ).toBeVisible()
  await expect(
    chatOption.getByRole('status', { name: 'В диалоге выполняется процесс' }),
  ).toHaveCount(0)

  const title = workingOption.locator('.panel-item-title')
  const bounds = await title.boundingBox()
  const indicators = await title.locator('.activity-indicators').boundingBox()
  expect(
    Math.abs(bounds!.x + bounds!.width - indicators!.x - indicators!.width),
  ).toBeLessThan(1)

  activity = { projects: {}, chats: {} }
  await expect(workingOption.getByRole('status')).toHaveCount(0)
  await expect(chatOption.getByRole('status')).toHaveCount(0)
})
