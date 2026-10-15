import { defineConfig } from '@playwright/test'
import { existsSync } from 'node:fs'
const channel = process.env.PRA_E2E_BROWSER_CHANNEL || (process.platform === 'win32' && existsSync('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe') ? 'msedge' : undefined)
export default defineConfig({ testDir: './e2e', workers: 1, fullyParallel: false,
  timeout: 90000, expect: { timeout: 45000 }, reporter: 'list',
  use: { baseURL: process.env.PRA_E2E_URL || 'http://localhost:8080',
    viewport: { width: 1440, height: 1000 }, channel, headless: true, trace: 'off' } })
