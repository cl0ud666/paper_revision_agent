import { test, expect } from '@playwright/test'

test('completed result has Word explanation, no report UI or empty unfinished block', async ({
  page,
}) => {
  await page.route('**/api/v1/**', (route) => {
    const url = route.request().url()
    if (url.endsWith('/users/me'))
      return route.fulfill({
        json: { id: 'test', username: '测试用户', csrf_token: 'test', is_admin: false },
      })
    if (new URL(url).pathname.endsWith('/events')) return route.fulfill({ json: { events: [] } })
    return route.fulfill({
      json: {
        id: 'result',
        document_name: '论文.docx',
        request: '修改论文',
        status: 'completed',
        stage: 'report',
        summary: { written: 4, unfinished: [] },
        created_at: '2026-10-19T01:00:00Z',
      },
    })
  })
  await page.goto('/runs/result')
  await expect(
    page.getByRole('heading', {
      name: '你的修订文件已就绪，所有修订及未能写进去的修订均已在修订版 Word 里面添加了批注，可点击审阅-显示标记的最终状态进行查看。',
      exact: true,
    }),
  ).toBeVisible()
  await expect(page.getByRole('link', { name: '下载修订版 Word' })).toBeVisible()
  await expect(page.locator('body')).not.toContainText(
    /查看修改报告|下载报告|最终确认清单中没有未完成/,
  )
  await expect(page.getByRole('region', { name: '未完成的修改及原因' })).toHaveCount(0)
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 })
    const bounds = await page.locator('.result-card').evaluate((card) => {
      const heading = card.querySelector('h2')!
      return {
        headingRight: heading.getBoundingClientRect().right,
        cardRight: card.getBoundingClientRect().right,
        scrollWidth: document.documentElement.scrollWidth,
        viewport: window.innerWidth,
      }
    })
    expect(bounds.headingRight, JSON.stringify(bounds)).toBeLessThanOrEqual(bounds.cardRight)
    expect(bounds.scrollWidth, JSON.stringify(bounds)).toBeLessThanOrEqual(bounds.viewport)
  }
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.screenshot({ path: 'test-results/result-copy.png', fullPage: true })
})
