import type { Page } from '@playwright/test'
import { api } from './support'
import { randomUUID } from 'node:crypto'
import type { HarnessProfile } from '../../src/api/settings'

// Keep group persistence real; replace only the host-dependent native boundary.
export async function installedHarness(
  page: Page,
  kind: 'codex' | 'opencode' = 'opencode',
  models = ['test/first', 'test/second'],
) {
  let harness: HarnessProfile = await api(page, 'POST', '/harness_profiles', {
    name: `Installed ${kind} ${randomUUID()}`,
    harness_kind: kind,
    executable_path: `C:/tools/${kind}.exe`,
    settings: { permission_mode: kind === 'codex' ? 'read_only' : 'no_tools' },
  })
  harness = {
    ...harness,
    catalog_models: models,
    settings: { ...harness.settings, default_model: models[0] },
  }
  await page.route('**/api/harnesses/discover', (route) =>
    route.fulfill({ json: [harness] }),
  )
  await page.route(
    `**/api/harness_profiles/${harness.id}/models/refresh*`,
    (route) =>
      route.fulfill({
        json: {
          status: 'fresh',
          models: harness.catalog_models.map((id) => ({
            id,
            source: 'native_catalog',
            reasoning_efforts: [],
          })),
          fetched_at: new Date().toISOString(),
          ttl_seconds: 900,
        },
      }),
  )
  await page.route(`**/api/harness_profiles/${harness.id}`, async (route) => {
    if (route.request().method() === 'PATCH') {
      const update = route.request().postDataJSON()
      if (update.expected_version !== harness.version) {
        await route.fulfill({
          status: 409,
          json: {
            code: 'version_conflict',
            message: 'Harness изменена',
            details: {},
            request_id: 'fixture',
            retryable: false,
          },
        })
        return
      }
      harness = {
        ...harness,
        settings: update.settings,
        version: harness.version + 1,
      }
    }
    await route.fulfill({ json: harness })
  })
  return harness
}
