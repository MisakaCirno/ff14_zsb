import { beforeEach, describe, expect, it, vi } from 'vitest'

import { initializeShareDetailDialog } from './share-detail-dialog'

function renderShell(): {
  cardButton: HTMLButtonElement
  dialog: HTMLDialogElement
  link: HTMLAnchorElement
} {
  document.body.innerHTML = `
    <a href="/s/share-a" data-share-detail-trigger>打开详情</a>
    <article data-share-card data-share-id="share-a">
      <button
        id="btn-like-share-a"
        class="btn btn-outline-danger"
        value="active"
        data-share-interaction="like"
        aria-label="点赞，当前 2 个点赞"
        aria-pressed="false"
        hx-vals='{"target_state":"active"}'>
        <i class="bi bi-heart"></i>
        <span data-reaction-count>2</span>
      </button>
    </article>
    <dialog data-share-detail-dialog aria-label="分享详情">
      <div data-share-detail-dialog-content></div>
    </dialog>
  `
  const dialog = document.querySelector<HTMLDialogElement>('dialog')!
  Object.defineProperty(dialog, 'open', {
    configurable: true,
    value: false,
    writable: true,
  })
  dialog.showModal = vi.fn(() => {
    dialog.open = true
  })
  dialog.close = vi.fn(() => {
    dialog.open = false
  })
  return {
    cardButton: document.querySelector<HTMLButtonElement>('#btn-like-share-a')!,
    dialog,
    link: document.querySelector<HTMLAnchorElement>('[data-share-detail-trigger]')!,
  }
}

const overlayMarkup = `
  <article
    data-share-detail
    data-share-detail-overlay
    data-share-id="share-a"
    data-share-title="测试分享"
    data-content-revealed="true">
    <button type="button" data-share-detail-dialog-close>关闭</button>
    <button
      id="btn-like-overlay-share-a"
      class="btn btn-danger"
      value="inactive"
      data-share-interaction="like"
      aria-label="点赞，当前 3 个点赞"
      aria-pressed="true"
      hx-vals='{"target_state":"inactive"}'>
      <i class="bi bi-heart-fill"></i>
      <span data-reaction-count>3</span>
    </button>
  </article>
`

describe('share detail dialog', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '/')
  })

  it('opens a same-origin detail as an overlay and pushes the canonical URL', async () => {
    const { dialog, link } = renderShell()
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      redirected: false,
      text: vi.fn().mockResolvedValue(overlayMarkup),
    })
    vi.stubGlobal('fetch', fetchMock)
    initializeShareDetailDialog()

    link.click()

    await vi.waitFor(() => {
      expect(dialog.querySelector('[data-share-detail-overlay]')).not.toBeNull()
    })
    expect(dialog.showModal).toHaveBeenCalledOnce()
    expect(fetchMock).toHaveBeenCalledWith(
      'http://localhost:3000/s/share-a?presentation=overlay',
      expect.objectContaining({
        credentials: 'same-origin',
        headers: {
          Accept: 'text/html',
          'HX-Request': 'true',
        },
      }),
    )
    expect(window.location.pathname).toBe('/s/share-a')
    expect(window.history.state).toEqual({
      shareDetailOverlay: true,
      shareUrl: 'http://localhost:3000/s/share-a',
    })
  })

  it('syncs changed reactions to the card and closes through browser history', async () => {
    const { cardButton, dialog, link } = renderShell()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      redirected: false,
      text: vi.fn().mockResolvedValue(overlayMarkup),
    }))
    const back = vi.spyOn(window.history, 'back').mockImplementation(() => undefined)
    initializeShareDetailDialog()
    link.click()
    await vi.waitFor(() => {
      expect(dialog.querySelector('[data-share-detail-dialog-close]')).not.toBeNull()
    })

    dialog.querySelector<HTMLButtonElement>('[data-share-detail-dialog-close]')!.click()

    expect(dialog.close).toHaveBeenCalledOnce()
    expect(back).toHaveBeenCalledOnce()
    expect(cardButton.getAttribute('aria-pressed')).toBe('true')
    expect(cardButton.value).toBe('inactive')
    expect(cardButton.classList.contains('btn-danger')).toBe(true)
    expect(cardButton.querySelector('[data-reaction-count]')?.textContent).toBe('3')
    expect(document.activeElement).toBe(link)
  })

  it('keeps modified and external-style clicks as normal link navigation', async () => {
    const { link } = renderShell()
    link.href = '#modified-navigation'
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    initializeShareDetailDialog()
    document.addEventListener('click', event => event.preventDefault(), { once: true })
    const event = new MouseEvent('click', {
      bubbles: true,
      cancelable: true,
      ctrlKey: true,
    })

    link.dispatchEvent(event)

    expect(event.defaultPrevented).toBe(true)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('maps Escape cancellation to the same history-aware close path', async () => {
    const { dialog, link } = renderShell()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      redirected: false,
      text: vi.fn().mockResolvedValue(overlayMarkup),
    }))
    const back = vi.spyOn(window.history, 'back').mockImplementation(() => undefined)
    initializeShareDetailDialog()
    link.click()
    await vi.waitFor(() => {
      expect(dialog.querySelector('[data-share-detail-overlay]')).not.toBeNull()
    })
    const cancelEvent = new Event('cancel', { cancelable: true })

    dialog.dispatchEvent(cancelEvent)

    expect(cancelEvent.defaultPrevented).toBe(true)
    expect(dialog.close).toHaveBeenCalledOnce()
    expect(back).toHaveBeenCalledOnce()
  })
})
