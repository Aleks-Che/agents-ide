import { defineConfig } from '@playwright/test'
import path from 'node:path'

const python = path.resolve(
  '../backend/.venv',
  process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
)
const dataDir = path.resolve('../.local/e2e-data')

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
