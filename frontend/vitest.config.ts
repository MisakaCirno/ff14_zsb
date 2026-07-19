import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vitest/config'

export default defineConfig({
  cacheDir: fileURLToPath(new URL('node_modules/.vite', import.meta.url)),
  root: fileURLToPath(new URL('.', import.meta.url)),
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.ts'],
    setupFiles: ['./src/test/setup.ts'],
  },
})
