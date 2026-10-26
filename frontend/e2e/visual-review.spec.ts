import { test, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

// All API responses are intercepted. No live account, jobs or model calls.
// Generate the two-page real Word/PDF fixture with scripts/verify_visual_review.py.
const root = resolve(import.meta.dirname, '../../data/verification-temp/visual-review')
const fixture = JSON.parse(readFileSync(resolve(root, 'review.json'), 'utf8'))
const pending = {
  id: 'pending-1',
  request: {
    type: 'final_confirm',
    visual: true,
    patches: fixture.final_patches,
    preview: fixture.preview_manifest,
    unavailable_count: 1,
    manual_items: [
      {
        manual_id: 'm1',
        title: '补引用',
        reason: '缺少可靠来源',
        suggestion: '请作者补充真实引用',
      },
    ],
  },
}
const base = {
  id: 'visual-test',
  document_name: '分页审阅验证样稿.docx',
  request: '全面检查并修改这篇论文',
  status: 'waiting_user',
  stage: 'waiting_user',
  version: 2,
  used_tokens: 1200,
  token_limit: 400000,
  pending,
  summary: {},
  error: '',
  has_result: false,
  has_report: false,
  created_at: '2026-10-15T01:00:00Z',
  updated_at: '2026-10-15T01:00:00Z',
}

test('page cards, arrows, defaults, select none/all and exact submission', async ({ page }) => {
  let submitted: any = null
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname.includes('/preview/'))
      return route.fulfill({
        contentType: 'image/png',
        body: readFileSync(
          resolve(
            root,
            `preview/${fixture.preview_manifest.fingerprint}/page-${url.pathname.split('/').pop()}.png`,
          ),
        ),
      })
    if (url.pathname.endsWith('/users/me'))
      return route.fulfill({
        json: { id: 'test-user', username: '审阅测试', is_admin: false, csrf_token: 'test' },
      })
    if (url.pathname.endsWith('/events')) return route.fulfill({ json: { events: [] } })
    if (url.pathname.endsWith('/decisions')) {
      submitted = route.request().postDataJSON()
      return route.fulfill({ json: {}, status: 202 })
    }
    return route.fulfill({ json: base })
  })
  await page.goto('/runs/visual-test')
  await expect(page.getByRole('checkbox', { name: '采用第 1 项修改' })).toBeChecked()
  await expect(page.getByRole('checkbox', { name: '采用第 2 项修改' })).toBeChecked()
  await expect(page.getByRole('button', { name: '确认并生成修订版' })).toBeEnabled()
  await expect(page.getByLabel('预览缩放')).toHaveValue('95')
  await expect(page.locator('body')).not.toContainText('小屏自动适配整页')
  await expect(
    page.getByRole('complementary', { name: '审阅边栏' }).getByLabel('原稿页码'),
  ).toBeVisible()
  const initialWidth = (await page.locator('.review-paper').boundingBox())!.width
  await page.getByLabel('预览缩放').selectOption('150')
  await expect
    .poll(async () => (await page.locator('.review-paper').boundingBox())!.width)
    .toBeGreaterThan(initialWidth * 1.5)
  await expect(page.getByRole('checkbox', { name: '采用第 1 项修改' })).toBeChecked()
  await page.getByRole('button', { name: '缩小原稿' }).click()
  await expect(page.getByLabel('预览缩放')).toHaveValue('145')
  await page.getByRole('button', { name: '整页', exact: true }).click()
  await expect(page.getByLabel('预览缩放')).toHaveValue('95')
  const controls = await page.evaluate(() => ({
    pageBottom: document.querySelector('.page-controls')!.getBoundingClientRect().bottom,
    zoomBottom: document.querySelector('.zoom-controls')!.getBoundingClientRect().bottom,
    selectTop: document
      .querySelector('.review-sidebar-controls > .button-row')!
      .getBoundingClientRect().top,
    sidebarWidth: document.querySelector('.review-sidebar')!.getBoundingClientRect().width,
    boardWidth: document.querySelector('.review-board')!.getBoundingClientRect().width,
  }))
  expect(controls.pageBottom).toBeLessThan(controls.selectTop)
  expect(controls.zoomBottom).toBeLessThan(controls.selectTop)
  expect(controls.sidebarWidth / controls.boardWidth).toBeGreaterThanOrEqual(0.39)
  const navigationOffsets = await page
    .locator('.page-controls button')
    .evaluateAll((buttons) =>
      buttons.map((button) => new DOMMatrix(getComputedStyle(button).transform).m42),
    )
  expect(navigationOffsets).toHaveLength(2)
  navigationOffsets.forEach((offset) => expect(offset).toBeCloseTo((-0.4 * 96) / 2.54, 2))
  await page.getByLabel('预览缩放').selectOption('50')
  await expect
    .poll(() =>
      page.locator('.review-paper-scroll').evaluate((pane) => {
        const paper = pane.querySelector('.review-paper')!.getBoundingClientRect(),
          outer = pane.getBoundingClientRect()
        return Math.abs(outer.right - paper.right - (96 / 2.54 + 2))
      }),
    )
    .toBeLessThan(1)
  await page.getByRole('button', { name: '整页', exact: true }).click()
  const manual = page.getByRole('region', { name: '需人工修改的事项' })
  for (const text of ['无法修改的原因：', '修改建议：', '原因：']) {
    await expect(manual.locator('strong').filter({ hasText: new RegExp(`^${text}$`) })).toHaveCount(
      1,
    )
  }
  for (const viewport of [
    { width: 1440, height: 1000 },
    { width: 1280, height: 720 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport)
    await expect
      .poll(() =>
        page.locator('.review-paper-scroll').evaluate((pane) => {
          const paper = pane.querySelector('.review-paper')!.getBoundingClientRect(),
            outer = pane.getBoundingClientRect()
          return (
            paper.width > 0 &&
            paper.left >= outer.left &&
            paper.right <= outer.right &&
            paper.top >= outer.top &&
            paper.bottom <= outer.bottom &&
            pane.scrollHeight === pane.clientHeight &&
            pane.scrollWidth === pane.clientWidth
          )
        }),
      )
      .toBeTruthy()
  }
  await page.setViewportSize({ width: 1440, height: 1000 })
  const layout = await page.evaluate(() => ({
    board: document.querySelector('.review-board')!.getBoundingClientRect().bottom,
    manual: document.querySelector('.unfinished-box')!.getBoundingClientRect().top,
  }))
  expect(layout.manual).toBeGreaterThan(layout.board)
  await expect(page.locator('body')).not.toContainText('token')
  await expect(page.locator('.review-arrow > path')).toHaveAttribute('d', /^M /)
  await page.locator('.review-workspace').evaluate((el) => el.scrollIntoView({ block: 'start' }))
  const visiblePaper = await page.locator('.review-paper').boundingBox()
  expect(visiblePaper!.y).toBeGreaterThanOrEqual(0)
  expect(visiblePaper!.y + visiblePaper!.height).toBeLessThanOrEqual(1000)
  await page.screenshot({ path: 'test-results/visual-review-viewport.png' })
  await page.screenshot({ path: 'test-results/visual-review-desktop.png', fullPage: true })
  await page.getByRole('button', { name: '定位第 2 页 ↗' }).click()
  await expect(page.getByLabel('原稿页码')).toHaveValue('2')
  await expect(page.getByRole('button', { name: '原文修改 2', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '原文修改 2', exact: true }).click()
  await expect(page.locator('.review-card.focused')).toContainText('修改 02')
  await page.getByRole('button', { name: '全不选', exact: true }).click()
  await expect(page.getByRole('checkbox', { name: '采用第 1 项修改' })).not.toBeChecked()
  await expect(page.getByText('已选 0 / 2 处')).toBeVisible()
  await page.getByRole('button', { name: '全选', exact: true }).click()
  await page.getByRole('checkbox', { name: '采用第 1 项修改' }).uncheck()
  await page.getByRole('button', { name: '确认并生成修订版' }).click()
  await expect.poll(() => submitted).not.toBeNull()
  expect(submitted).toEqual({
    pending_id: 'pending-1',
    version: 2,
    payload: { excluded_ids: [fixture.final_patches[0].patch_id] },
  })
  await page.setViewportSize({ width: 390, height: 844 })
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
  ).toBeTruthy()
  await page.screenshot({ path: 'test-results/visual-review-mobile.png', fullPage: true })
})

test('result has a boxed unfinished list with reasons', async ({ page }) => {
  await page.route('**/api/v1/**', async (route) => {
    const url = route.request().url()
    const json = url.endsWith('/users/me')
      ? { id: 'test-user', username: '审阅测试' }
      : new URL(url).pathname.endsWith('/events')
        ? { events: [] }
        : {
            ...base,
            status: 'completed',
            pending: null,
            summary: {
              written: 1,
              excluded: 1,
              unfinished: [
                {
                  title: 'MoCo → MOCO',
                  reason: '用户取消勾选，保留原文',
                  position: 'P01',
                  kind: 'not_selected',
                },
                {
                  title: '实验结论的比较对象',
                  reason: '缺少依据，需要作者核对',
                  kind: 'unresolved',
                },
              ],
            },
          }
    return route.fulfill({ json })
  })
  await page.goto('/runs/visual-test')
  const box = page.getByRole('region', { name: '未完成的修改及原因' })
  await expect(box).toBeVisible()
  await expect(box).not.toContainText('缺少依据，需要作者核对')
  await expect(box).toContainText('用户取消勾选，保留原文')
  await page.screenshot({ path: 'test-results/visual-review-result.png', fullPage: true })
})

test('failed page loading prevents blind confirmation', async ({ page }) => {
  await page.route('**/api/v1/**', async (route) => {
    const url = route.request().url()
    if (url.includes('/preview/')) return route.fulfill({ status: 503 })
    return route.fulfill({
      json: url.endsWith('/users/me')
        ? { username: '测试' }
        : new URL(url).pathname.endsWith('/events')
          ? { events: [] }
          : base,
    })
  })
  await page.goto('/runs/visual-test')
  await expect(page.getByText('页面加载失败，请重试后再确认。')).toBeVisible()
  await expect(page.getByRole('button', { name: '确认并生成修订版' })).toBeDisabled()
})

test('scrolling a long left list keeps the whole right page fixed', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 })
  const patches = Array.from({ length: 30 }, (_, i) => ({
    ...fixture.final_patches[0],
    patch_id: `item-${i}`,
    number: i + 1,
  }))
  await page.route('**/api/v1/**', (route) => {
    const url = new URL(route.request().url())
    if (url.pathname.includes('/preview/'))
      return route.fulfill({
        contentType: 'image/png',
        body: readFileSync(
          resolve(root, `preview/${fixture.preview_manifest.fingerprint}/page-1.png`),
        ),
      })
    return route.fulfill({
      json: url.pathname.endsWith('/users/me')
        ? { username: '测试' }
        : url.pathname.endsWith('/events')
          ? { events: [] }
          : { ...base, pending: { ...pending, request: { ...pending.request, patches } } },
    })
  })
  await page.goto('/runs/visual-test')
  await expect(page.getByRole('button', { name: '确认并生成修订版' })).toBeEnabled()
  const before = await page.locator('.review-paper').boundingBox()
  expect(before!.y).toBeGreaterThanOrEqual(0)
  expect(before!.y + before!.height).toBeLessThanOrEqual(720)
  await page.locator('.review-cards').hover()
  await page.mouse.wheel(0, 900)
  await expect
    .poll(() => page.locator('.review-cards').evaluate((el) => el.scrollTop))
    .toBeGreaterThan(0)
  expect(await page.locator('.review-paper').boundingBox()).toEqual(before)
  expect(await page.locator('.review-paper-scroll').evaluate((el) => el.scrollTop)).toBe(0)
})
