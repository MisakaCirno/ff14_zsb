import { defineConfig, devices } from '@playwright/test'

const baseURL = process.env.PLAYWRIGHT_BASE_URL
if (!baseURL) {
  throw new Error('PLAYWRIGHT_BASE_URL is required for isolated browser tests.')
}

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  expect: {
    timeout: 5_000,
  },
  reporter: [['line']],
  outputDir: process.env.PLAYWRIGHT_OUTPUT_DIR ?? '../coverage/playwright',
  use: {
    baseURL,
    headless: true,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
})
