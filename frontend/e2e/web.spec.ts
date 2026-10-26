import { test, expect, type Page, type APIRequestContext } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { execFileSync } from 'node:child_process'
import { unzipSync } from 'fflate'

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
const username = `qa-${Date.now()}`,
  password = 'qa-local-test-password-123'
let userId = '',
  admin: APIRequestContext
const fixture = execFileSync(
  process.platform === 'win32' ? resolve(root, '.venv/Scripts/python.exe') : 'python',
  [
    '-c',
    'from tests.test_web_api import paper_bytes; import sys; sys.stdout.buffer.write(paper_bytes())',
  ],
  { cwd: root },
)

test.beforeAll(async ({ playwright }) => {
  admin = await playwright.request.newContext({
    baseURL: process.env.PRA_E2E_URL || 'http://localhost:8080',
  })
  const response = await admin.post('/api/v1/auth/login', {
    data: { username: config.PRA_ADMIN_USERNAME, password: config.PRA_ADMIN_PASSWORD },
  })
  expect(response.ok()).toBeTruthy()
  const me = await response.json()
  await admin.dispose()
  // 保留会话 Cookie，使用独立请求上下文设置 CSRF Header。
  const authenticated = await playwright.request.newContext({
    baseURL: process.env.PRA_E2E_URL || 'http://localhost:8080',
  })
  const second = await authenticated.post('/api/v1/auth/login', {
    data: { username: config.PRA_ADMIN_USERNAME, password: config.PRA_ADMIN_PASSWORD },
  })
  const state = await authenticated.storageState()
  const csrf = (await second.json()).csrf_token
  admin = await playwright.request.newContext({
    baseURL: process.env.PRA_E2E_URL || 'http://localhost:8080',
    storageState: state,
    extraHTTPHeaders: { 'X-CSRF-Token': csrf },
  })
  await authenticated.dispose()
  const created = await admin.post('/api/v1/admin/users', {
    data: { username, password, daily_run_limit: 100, run_token_limit: 400000 },
  })
  expect(created.status()).toBe(201)
  userId = (await created.json()).id
})

test.afterAll(async () => {
  if (userId) await admin.patch(`/api/v1/admin/users/${userId}`, { data: { active: false } })
  await admin?.dispose()
})

async function login(page: Page) {
  await page.goto('/')
  await page.getByLabel('账号', { exact: true }).fill(username)
  await page.getByLabel('密码', { exact: true }).fill(password)
  await page.getByRole('button', { name: '进入工作台' }).click()
  await expect(page.getByRole('button', { name: '上传论文', exact: true })).toBeVisible()
}

async function upload(page: Page) {
  await page.getByRole('button', { name: '上传论文', exact: true }).click()
  await page.getByLabel('选择论文文件').setInputFiles({
    name: 'MoCo-术语验收.docx',
    mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    buffer: fixture,
  })
  await page.getByRole('button', { name: '提交并开始处理' }).click()
  await expect(page).toHaveURL(/\/runs\//)
}

test('上传 → 术语选择 → 关闭页面与重启 Worker → 最终确认 → 下载修订 Word', async ({
  page,
  context,
}) => {
  await login(page)
  await upload(page)
  await expect(page.getByText('这些口径，需要由你决定')).toBeVisible()
  const url = page.url()
  execFileSync(
    'docker',
    [
      'compose',
      '--env-file',
      'deploy/.env',
      '-f',
      'deploy/compose.yml',
      '-f',
      'deploy/compose.demo.yml',
      'restart',
      'worker',
    ],
    { cwd: root, timeout: 60000, stdio: 'pipe' },
  )
  await page.close()
  const reopened = await context.newPage()
  await reopened.goto(url)
  await reopened.getByRole('radio', { name: /统一为.*MOCO/ }).check()
  await reopened.getByRole('button', { name: '确认选择并继续' }).click()
  await expect(reopened.getByText('生成文件前，最后确认一次')).toBeVisible()
  await reopened.getByRole('button', { name: '确认并生成修订版' }).click()
  await expect(reopened.getByRole('link', { name: '下载修订版 Word' })).toBeVisible()
  const [download] = await Promise.all([
    reopened.waitForEvent('download'),
    reopened.getByRole('link', { name: '下载修订版 Word' }).click(),
  ])
  const document = unzipSync(readFileSync((await download.path())!))
  expect(new TextDecoder().decode(document['word/document.xml'])).toContain('<w:ins')
  await reopened.getByRole('button', { name: '查看修改报告' }).click()
  await expect(reopened.locator('.report pre')).toContainText('学术论文修改报告')
  await reopened.screenshot({ path: 'test-results/task-completed.png', fullPage: true })
})

test('Redis 短暂停止时仍可上传，恢复后 outbox 自动投递', async ({ page }) => {
  await login(page)
  try {
    execFileSync(
      'docker',
      ['compose', '--env-file', 'deploy/.env', '-f', 'deploy/compose.yml', 'stop', 'redis'],
      { cwd: root, timeout: 60000, stdio: 'pipe' },
    )
    await upload(page)
    await expect(page.getByText('排队中', { exact: true })).toBeVisible()
  } finally {
    execFileSync(
      'docker',
      ['compose', '--env-file', 'deploy/.env', '-f', 'deploy/compose.yml', 'start', 'redis'],
      { cwd: root, timeout: 60000, stdio: 'pipe' },
    )
  }
  await expect(page.getByText('这些口径，需要由你决定')).toBeVisible()
})

test('窄屏工作台可以正常展示和上传', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await login(page)
  await page.getByRole('button', { name: '上传论文', exact: true }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
  ).toBeTruthy()
  await page.screenshot({ path: 'test-results/mobile-upload.png', fullPage: true })
})
