import { defineConfig } from '@playwright/test'
import path from 'node:path'
import { randomUUID } from 'node:crypto'

const python = path.resolve(
  '../backend/.venv',
  process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
)
const dataDir =
  process.env.AGENTS_IDE_E2E_DATA_DIR ??
  path.resolve(`../.local/e2e-${randomUUID()}`)
process.env.AGENTS_IDE_E2E_DATA_DIR = dataDir

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: 'http://127.0.0.1:18767',
    browserName: 'chromium',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: `"${python}" -m agents_ide --data-dir "${dataDir}" --port 18767 api`,
    url: 'http://127.0.0.1:18767/api/health',
    reuseExistingServer: false,
    timeout: 30_000,
  },
})
