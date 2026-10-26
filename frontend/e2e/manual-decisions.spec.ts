import { test, expect } from '@playwright/test'

const manual = {
  manual_id: 'manual-1',
  kind: 'manual',
  title: '补充 BERT 对比实验',
  position: '5.3 与其他模型对比',
  blocked_reason: '需要作者实际开展实验并提供真实结果',
  suggestion: '补充 BERT 对比实验，保留描述',
  reason: '总结的比较缺少对应实验依据',
}
const base = {
  id: 'decision-test',
  document_name: '论文.docx',
  request: '全面修改',
  status: 'waiting_user',
  stage: 'waiting_user',
  version: 2,
  used_tokens: 100,
  token_limit: 10000,
  summary: {},
  error: '',
  has_result: false,
  has_report: false,
  created_at: '2026-10-17T00:00:00Z',
  updated_at: '2026-10-17T00:00:00Z',
}

test('manual-only final confirmation is visible, submittable and retains instructions', async ({
  page,
}) => {
  let submitted: any = null
  await page.route('**/api/v1/**', (route) => {
    const url = route.request().url()
    if (url.endsWith('/users/me'))
      return route.fulfill({ json: { username: '测试', csrf_token: 'test' } })
    if (new URL(url).pathname.endsWith('/events')) return route.fulfill({ json: { events: [] } })
    if (url.endsWith('/decisions')) {
      submitted = route.request().postDataJSON()
      return route.fulfill({ json: {}, status: 202 })
    }
    return route.fulfill({
      json: submitted
        ? { ...base, status: 'completed', pending: null, summary: { unfinished: [manual] } }
        : {
            ...base,
            pending: {
              id: 'pending-1',
              request: {
                type: 'final_confirm',
                visual: true,
                patches: [],
                manual_items: [manual],
                preview: { pages: [] },
              },
            },
          },
    })
  })
  await page.goto('/runs/decision-test')
  const box = page.getByRole('region', { name: '需人工修改的事项' })
  await expect(box).toContainText('无法修改，需人工修改')
  await expect(box).toContainText('修改建议：补充 BERT 对比实验，保留描述')
  await page.screenshot({ path: 'test-results/manual-before-confirm.png', fullPage: true })
  await page.getByRole('button', { name: '确认并生成修订版' }).click()
  await expect.poll(() => submitted).not.toBeNull()
  expect(submitted.payload.excluded_ids).toEqual([])
  await expect(page.getByRole('region', { name: '未完成的修改及原因' })).toContainText(
    manual.blocked_reason,
  )
})

test('choice B submits its exact index and uses section names', async ({ page }) => {
  let submitted: any = null
  await page.route('**/api/v1/**', (route) => {
    const url = route.request().url()
    if (url.endsWith('/users/me'))
      return route.fulfill({ json: { username: '测试', csrf_token: 'test' } })
    if (new URL(url).pathname.endsWith('/events')) return route.fulfill({ json: { events: [] } })
    if (url.endsWith('/decisions')) {
      submitted = route.request().postDataJSON()
      return route.fulfill({ json: {}, status: 202 })
    }
    return route.fulfill({
      json: {
        ...base,
        pending: {
          id: 'pending-choice',
          request: {
            type: 'issue_review',
            issues: [
              {
                id: 7,
                problem: 'P150 对比缺少依据',
                span_label: 'P150',
                options: ['删除 BERT 描述', manual.suggestion],
                display: {
                  problem: '章节小结的对比缺少依据',
                  position: manual.position,
                  display_options: ['删除 BERT 描述', manual.suggestion],
                },
              },
            ],
          },
        },
      },
    })
  })
  await page.goto('/runs/decision-test')
  await expect(page.locator('.issue')).not.toContainText('P150')
  await expect(page.locator('.issue')).toContainText(manual.position)
  await page.getByRole('radio', { name: manual.suggestion, exact: true }).check()
  await page.getByRole('button', { name: '确认选择并继续' }).click()
  await expect.poll(() => submitted).not.toBeNull()
  expect(submitted.payload.choices).toEqual({ '7': 1 })
})
