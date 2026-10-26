import { test, expect } from '@playwright/test'

const user = {
  id: 'trial-user',
  username: 'newuser',
  is_admin: false,
  daily_run_limit: 5,
  csrf_token: 'test',
  free_quota: { total: 3, completed: 0, reserved: 0, remaining: 3 },
}

test('simple registration without quota, phone, or usage copy', async ({ page }) => {
  let loggedIn = false,
    body: any = null
  await page.route('**/api/v1/**', (route) => {
    const url = route.request().url()
    if (url.endsWith('/auth/options'))
      return route.fulfill({ json: { registration_open: true, free_runs: 3 } })
    if (url.endsWith('/auth/register')) {
      loggedIn = true
      body = route.request().postDataJSON()
      return route.fulfill({ status: 201, json: user })
    }
    if (url.endsWith('/users/me'))
      return route.fulfill(loggedIn ? { json: user } : { status: 401, json: { detail: '请登录' } })
    return route.fulfill({ json: [] })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '没有账号？立即注册' }).click()
  await page.getByLabel('账号', { exact: true }).fill('newuser')
  await expect(page.getByLabel('密码', { exact: true })).toHaveAttribute('minlength', '6')
  await expect(page.getByText('面向研究者的论文修改智能体', { exact: true })).toBeVisible()
  const sizes = await page.evaluate(() => ({
    tag: parseFloat(getComputedStyle(document.querySelector('.login-story > small')!).fontSize),
    steps: parseFloat(getComputedStyle(document.querySelector('.story-steps')!).fontSize),
  }))
  expect(sizes.tag).toBeGreaterThan(sizes.steps)
  await page.getByLabel('密码', { exact: true }).fill('123456')
  await page.getByLabel('确认密码', { exact: true }).fill('different')
  await expect(page.getByRole('button', { name: '注册并进入工作台' })).toBeDisabled()
  await page.getByLabel('确认密码', { exact: true }).fill('123456')
  await page.screenshot({ path: 'test-results/simple-register.png', fullPage: true })
  await page.getByRole('button', { name: '注册并进入工作台' }).click()
  expect(body).toEqual({ username: 'newuser', password: '123456' })
  await expect(page.locator('body')).not.toContainText(/免费|收费|机会|token|手机号|短信/)
})

test('exhausted opportunities disable upload without quota copy', async ({ page }) => {
  await page.route('**/api/v1/**', (route) =>
    route.fulfill({
      json: route.request().url().endsWith('/users/me')
        ? { ...user, free_quota: { total: 3, completed: 2, reserved: 1, remaining: 0 } }
        : [],
    }),
  )
  await page.goto('/')
  await expect(page.getByRole('button', { name: '上传论文', exact: true })).toBeDisabled()
  await expect(page.locator('body')).not.toContainText(/免费|收费|机会/)
  await page.screenshot({ path: 'test-results/trial-quota.png', fullPage: true })
})
