// @vitest-environment jsdom

import { describe, expect, it, vi } from 'vitest'

describe('share copy counter', () => {
  it('restores the copy button with the server count after success feedback', async () => {
    vi.useFakeTimers()
    document.head.innerHTML = '<meta name="csrf-token" content="test-token">'
    document.body.innerHTML = `
      <div id="message-container"></div>
      <article data-share-card>
        <button
          type="button"
          data-copy-strategy
          data-copy-label="复制分享码"
          aria-label="复制分享码，当前已复制 2 次">
          <i class="bi bi-clipboard" aria-hidden="true"></i>
          <span data-copies-count>2</span>
        </button>
      </article>
    `
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: vi.fn(async () => undefined) },
    })
    vi.stubGlobal('fetch', vi.fn(async () => ({
      json: async () => ({ copies_count: 3 }),
      ok: true,
    })))
    const { performShareCopy } = await import('./share-copy')
    const button = document.querySelector<HTMLButtonElement>('button')!

    await performShareCopy({
      button,
      manualLabel: '战术板代码',
      recordUrl: '/shares/example/copy/',
      successMessage: '已复制',
      text: '[stgy:test]',
    })
    await Promise.resolve()
    await Promise.resolve()
    await vi.advanceTimersByTimeAsync(2000)

    expect(button.querySelector('[data-copies-count]')?.textContent).toBe('3')
    expect(button.getAttribute('aria-label')).toBe('复制分享码，当前已复制 3 次')
  })
})
