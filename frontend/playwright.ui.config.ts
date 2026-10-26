import { defineConfig } from '@playwright/test'

// Offline UI checks: every API response is mocked by the selected specs.
// The live-service and LibreOffice fixture suites retain their separate config.
export default defineConfig({
  testDir: './e2e',
  testMatch: ['free-trial.spec.ts', 'manual-decisions.spec.ts', 'result-copy.spec.ts'],
  workers: 1,
  fullyParallel: false,
  timeout: 30000,
  use: {
    baseURL: 'http://127.0.0.1:4173',
    viewport: { width: 1440, height: 1000 },
    channel: process.env.PRA_E2E_BROWSER_CHANNEL || undefined,
    headless: true,
  },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 4173 --strictPort',
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: false,
  },
})
