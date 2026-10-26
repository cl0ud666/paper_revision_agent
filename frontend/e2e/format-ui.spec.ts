import { test, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

// Read-only UI smoke: no uploads, new accounts, queue changes or model calls.
const root = resolve(import.meta.dirname, '../..')
const config = Object.fromEntries(
  readFileSync(resolve(root, 'deploy/.env'), 'utf8')
    .split(/\r?\n/)
    .filter((l) => l && !l.startsWith('#'))
    .map((l) => {
      const i = l.indexOf('=')
      return [l.slice(0, i), l.slice(i + 1)]
    }),
)

test('Word-family upload formats and conversion notice are visible', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('账号', { exact: true }).fill(config.PRA_ADMIN_USERNAME)
  await page.getByLabel('密码', { exact: true }).fill(config.PRA_ADMIN_PASSWORD)
  await page.getByRole('button', { name: '进入工作台' }).click()
  await page.getByRole('button', { name: '上传论文', exact: true }).click()
  await expect(page.getByLabel('选择论文文件')).toHaveAttribute(
    'accept',
    '.docx,.doc,.wps,.rtf,.odt',
  )
  await expect(page.getByText(/非 DOCX 文件会在后台转换/)).toBeVisible()
  await page.setViewportSize({ width: 390, height: 844 })
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
  ).toBeTruthy()
  await page.screenshot({ path: 'test-results/word-formats-mobile.png', fullPage: true })
})
