import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'

const STANDARD_SHARE_ID = '2a3b4c5d'
const SPOILER_SHARE_ID = '3e4f5g6h'
let runtimeErrors: string[] = []

function shareCard(page: Page, shareId: string) {
  return page.locator(`[data-share-card][data-share-id="${shareId}"]`)
}

async function expectNoWcagViolations(page: Page) {
  const results = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'])
    .analyze()

  expect(
    results.violations.map(({ help, id, nodes }) => ({
      help,
      id,
      targets: nodes.map((node) => node.target),
    })),
  ).toEqual([])
}

test.beforeEach(async ({ page }) => {
  runtimeErrors = []
  page.on('pageerror', (error) => runtimeErrors.push(error.message))
  // Board previews belong to the separately managed renderer and are outside
  // this test batch. Keep browser runs deterministic and offline.
  await page.route('**/n/**', (route) => route.fulfill({ status: 204 }))
})

test.afterEach(() => {
  expect(runtimeErrors).toEqual([])
})

test('three-state content preferences filter, mask, and reveal cards', async ({ page }) => {
  await page.goto('/?feed=paginated&spoiler=mask&nsfw=mask')

  const spoilerPreference = page.locator('#spoiler-preference')
  await expect(spoilerPreference.locator('option')).toHaveCount(3)
  await expect(shareCard(page, SPOILER_SHARE_ID)).toBeVisible()
  await expect(shareCard(page, SPOILER_SHARE_ID).locator('.spoiler-overlay')).toBeVisible()

  await spoilerPreference.selectOption('hide')
  await expect(page).toHaveURL(/spoiler=hide/)
  await expect(shareCard(page, SPOILER_SHARE_ID)).toHaveCount(0)
  await expect(shareCard(page, STANDARD_SHARE_ID)).toBeVisible()

  await page.locator('#spoiler-preference').selectOption('show')
  await expect(page).toHaveURL(/spoiler=show/)
  await expect(shareCard(page, SPOILER_SHARE_ID)).toBeVisible()
  await expect(shareCard(page, SPOILER_SHARE_ID).locator('.spoiler-overlay')).toHaveCount(0)
})

test('card opens an accessible detail dialog and restores focus when closed', async ({ page }) => {
  await page.goto('/?feed=paginated&spoiler=mask&nsfw=mask')

  const trigger = shareCard(page, SPOILER_SHARE_ID).locator('[data-share-detail-trigger]').first()
  await trigger.click()

  const dialog = page.locator('[data-share-detail-dialog]')
  await expect(dialog).toHaveAttribute('open', '')
  await expect(dialog.locator('[data-content-overlay]')).toBeVisible()
  await dialog.locator('[data-reveal-content]').click()
  await expect(dialog.locator('[data-share-detail]')).toHaveAttribute('data-content-revealed', 'true')
  await expect(dialog.locator('[data-content-overlay]')).toBeHidden()

  await page.keyboard.press('Escape')
  await expect(dialog).not.toHaveAttribute('open', '')
  await expect(trigger).toBeFocused()
})

test('login preserves the safe return path and exposes owned content', async ({ page }) => {
  await page.goto('/login/?next=/my-shares/')
  await page.locator('#id_username').fill('e2e-user')
  await page.locator('#id_password').fill('E2e-password-42')
  await page.locator('[data-account-form] button[type="submit"]').click()

  await expect(page).toHaveURL(/\/my-shares\/$/)
  await expect(page.locator('[data-managed-share]')).toHaveCount(3)
})

test('core public pages have no automatically detectable WCAG A or AA violations', async ({ page }) => {
  const paths = [
    '/?feed=paginated&spoiler=mask&nsfw=mask',
    '/login/',
    `/s/${STANDARD_SHARE_ID}`,
  ]

  for (const path of paths) {
    await page.goto(path)
    await expectNoWcagViolations(page)
  }
})

test('mobile homepage keeps primary controls reachable without horizontal overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/?feed=paginated&spoiler=mask&nsfw=mask')

  await expect(page.locator('[data-browse-toolbar]')).toBeVisible()
  await expect(page.locator('#spoiler-preference')).toBeVisible()
  await expect(page.locator('#nsfw-preference')).toBeVisible()
  const hasHorizontalOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
  )
  expect(hasHorizontalOverflow).toBe(false)
  await expectNoWcagViolations(page)
})
