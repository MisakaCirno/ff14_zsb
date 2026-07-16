import { beforeEach, describe, expect, it, vi } from 'vitest'

function renderAnnouncement(options: { navVisible?: boolean } = {}): {
  banner: HTMLElement
  closeButton: HTMLButtonElement
  navLink: HTMLAnchorElement
} {
  document.body.innerHTML = `
    <a id="nav-announcement-link" href="/announcements/">站点动态</a>
    <aside id="announcement-banner" data-announcement-id="42" style="display: none;">
      <h2 id="browse-announcement-title">维护通知</h2>
      <a href="/details/">查看详情</a>
      <button type="button" data-dismiss-announcement>关闭</button>
    </aside>
    <main id="main-content" tabindex="-1"></main>
  `
  const navLink = document.querySelector<HTMLAnchorElement>('#nav-announcement-link')!
  Object.defineProperty(navLink, 'offsetParent', {
    configurable: true,
    value: options.navVisible === false ? null : document.body,
  })
  return {
    banner: document.getElementById('announcement-banner')!,
    closeButton: document.querySelector<HTMLButtonElement>('[data-dismiss-announcement]')!,
    navLink,
  }
}

describe('announcement dismissal', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('skips the decorative flight animation when reduced motion is requested', async () => {
    vi.useFakeTimers()
    const matchMedia = vi.fn(() => ({ matches: true }))
    vi.stubGlobal('matchMedia', matchMedia)
    const requestAnimationFrame = vi.spyOn(window, 'requestAnimationFrame')
    const elements = renderAnnouncement()
    const { initializeAnnouncement } = await import('./announcement')

    initializeAnnouncement()
    expect(elements.banner.style.display).toBe('block')
    elements.closeButton.focus()
    elements.closeButton.click()

    expect(matchMedia).toHaveBeenCalledWith('(prefers-reduced-motion: reduce)')
    expect(requestAnimationFrame).not.toHaveBeenCalled()
    expect(elements.banner.style.display).toBe('none')
    expect(elements.banner.getAttribute('aria-hidden')).toBe('true')
    expect(elements.banner.hasAttribute('inert')).toBe(true)
    expect(document.querySelectorAll('[data-announcement-id="42"]')).toHaveLength(1)
    expect(document.activeElement).toBe(elements.navLink)
    expect(localStorage.getItem('dismissed_announcement_id')).toBe('42')
  })

  it('keeps the existing visual dismissal when reduced motion is not requested', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false })))
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
      callback(0)
      return 1
    })
    const elements = renderAnnouncement()
    const { initializeAnnouncement } = await import('./announcement')

    initializeAnnouncement()
    elements.closeButton.click()

    expect(document.querySelectorAll('[data-announcement-id="42"]')).toHaveLength(2)
    const clone = Array.from(document.querySelectorAll<HTMLElement>('[data-announcement-id="42"]'))
      .find((candidate) => candidate !== elements.banner)!
    expect(clone.getAttribute('aria-hidden')).toBe('true')
    expect(clone.hasAttribute('inert')).toBe(true)
    await vi.advanceTimersByTimeAsync(800)
    expect(document.querySelectorAll('[data-announcement-id="42"]')).toHaveLength(1)
  })

  it('makes the fading banner inert immediately when the navigation target is hidden', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false })))
    const elements = renderAnnouncement({ navVisible: false })
    const main = document.getElementById('main-content')!
    const { initializeAnnouncement } = await import('./announcement')

    initializeAnnouncement()
    elements.closeButton.focus()
    elements.closeButton.click()

    expect(elements.banner.style.display).toBe('block')
    expect(elements.banner.getAttribute('aria-hidden')).toBe('true')
    expect(elements.banner.hasAttribute('inert')).toBe(true)
    expect(elements.banner.style.transition).toContain('opacity 0.3s ease')
    expect(document.activeElement).toBe(main)

    await vi.advanceTimersByTimeAsync(300)
    expect(elements.banner.style.display).toBe('none')
  })
})
