import { beforeEach, describe, expect, it, vi } from 'vitest'

function renderAnnouncement(): {
  banner: HTMLElement
  closeButton: HTMLButtonElement
  dialog: HTMLDialogElement
  main: HTMLElement
  openButton: HTMLButtonElement
} {
  document.body.innerHTML = `
    <aside id="announcement-banner" data-announcement-banner data-announcement-id="42">
      <button id="announcement-open" type="button" data-announcement-open>维护通知</button>
      <dialog data-announcement-dialog data-announcement-id="42">
        <button type="button" data-announcement-dialog-close data-dismiss-announcement>关闭</button>
        <button type="button" data-dismiss-announcement>知道了</button>
      </dialog>
    </aside>
    <main id="main-content" tabindex="-1"></main>
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
    banner: document.querySelector<HTMLElement>('[data-announcement-banner]')!,
    closeButton: document.querySelector<HTMLButtonElement>('[data-announcement-dialog-close]')!,
    dialog,
    main: document.querySelector<HTMLElement>('#main-content')!,
    openButton: document.querySelector<HTMLButtonElement>('[data-announcement-open]')!,
  }
}

describe('announcement dialog', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.restoreAllMocks()
  })

  it('opens a new announcement automatically and remembers dismissal', async () => {
    const elements = renderAnnouncement()
    const { initializeAnnouncement } = await import('./announcement')

    initializeAnnouncement()

    expect(elements.dialog.showModal).toHaveBeenCalledOnce()
    expect(document.activeElement).toBe(elements.closeButton)
    elements.closeButton.click()

    expect(elements.dialog.close).toHaveBeenCalledOnce()
    expect(elements.banner.hidden).toBe(true)
    expect(document.activeElement).toBe(elements.main)
    expect(localStorage.getItem('dismissed_announcement_id')).toBe('42')
  })

  it('hides the homepage entry for an announcement that was already read', async () => {
    localStorage.setItem('dismissed_announcement_id', '42')
    const elements = renderAnnouncement()
    const { initializeAnnouncement } = await import('./announcement')

    initializeAnnouncement()
    expect(elements.dialog.showModal).not.toHaveBeenCalled()
    expect(elements.banner.hidden).toBe(true)
  })

  it('shows and opens a newer announcement after an older one was read', async () => {
    localStorage.setItem('dismissed_announcement_id', '41')
    const elements = renderAnnouncement()
    const { initializeAnnouncement } = await import('./announcement')

    initializeAnnouncement()

    expect(elements.banner.hidden).toBe(false)
    expect(elements.dialog.showModal).toHaveBeenCalledOnce()
  })

  it('maps Escape cancellation to the same persistent close path', async () => {
    const elements = renderAnnouncement()
    const { initializeAnnouncement } = await import('./announcement')

    initializeAnnouncement()
    const cancelEvent = new Event('cancel', { cancelable: true })
    elements.dialog.dispatchEvent(cancelEvent)

    expect(cancelEvent.defaultPrevented).toBe(true)
    expect(elements.dialog.close).toHaveBeenCalledOnce()
    expect(localStorage.getItem('dismissed_announcement_id')).toBe('42')
    expect(elements.banner.hidden).toBe(true)
    expect(document.activeElement).toBe(elements.main)
  })

  it('closes when the backdrop itself is clicked', async () => {
    const elements = renderAnnouncement()
    const { initializeAnnouncement } = await import('./announcement')

    initializeAnnouncement()
    elements.dialog.dispatchEvent(new MouseEvent('click', { bubbles: true }))

    expect(elements.dialog.close).toHaveBeenCalledOnce()
    expect(localStorage.getItem('dismissed_announcement_id')).toBe('42')
    expect(elements.banner.hidden).toBe(true)
  })
})
