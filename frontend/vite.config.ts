import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: `http://127.0.0.1:${process.env.AGENTS_IDE_PORT ?? '8765'}`,
        changeOrigin: true,
        // Keep the browser Origin: the API checks the explicitly configured dev origin.
        timeout: 0,
        proxyTimeout: 0,
      },
    },
  },
  test: { environment: 'node', include: ['tests/**/*.test.ts'] },
})
