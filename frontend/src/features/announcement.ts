const dismissedAnnouncementKey = 'dismissed_announcement_id'

function rememberDismissal(announcementId: string): void {
  if (!announcementId) {
    return
  }
  try {
    localStorage.setItem(dismissedAnnouncementKey, announcementId)
  } catch (error) {
    console.warn('Unable to persist the dismissed announcement.', error)
  }
}

function wasDismissed(announcementId: string): boolean {
  try {
    return localStorage.getItem(dismissedAnnouncementKey) === announcementId
  } catch (error) {
    console.warn('Unable to read the dismissed announcement.', error)
    return false
  }
}

function dismissAnnouncement(banner: HTMLElement): void {
  const navLink = document.getElementById('nav-announcement-link')
  const announcementId = banner.dataset.announcementId ?? ''
  const isNavLinkVisible = navLink instanceof HTMLElement && navLink.offsetParent !== null

  if (isNavLinkVisible) {
    const clone = banner.cloneNode(true) as HTMLElement
    const rect = banner.getBoundingClientRect()
    const targetRect = navLink.getBoundingClientRect()
    clone.style.position = 'fixed'
    clone.style.left = `${rect.left}px`
    clone.style.top = `${rect.top}px`
    clone.style.width = `${rect.width}px`
    clone.style.height = `${rect.height}px`
    clone.style.zIndex = '9999'
    clone.style.transition = 'all 0.8s cubic-bezier(0.68, -0.55, 0.265, 1.55)'
    clone.style.opacity = '1'
    clone.classList.remove('mb-4')
    clone.querySelector('[data-dismiss-announcement]')?.remove()
    document.body.appendChild(clone)
    banner.style.display = 'none'
    void clone.offsetHeight

    requestAnimationFrame(() => {
      clone.style.left = `${targetRect.left}px`
      clone.style.top = `${targetRect.top}px`
      clone.style.width = '20px'
      clone.style.height = '20px'
      clone.style.opacity = '0'
      clone.style.transform = 'scale(0.1)'
    })
    window.setTimeout(() => clone.remove(), 800)
  } else {
    banner.style.transition = 'opacity 0.3s ease, transform 0.3s ease'
    banner.style.opacity = '0'
    banner.style.transform = 'scale(0.9)'
    window.setTimeout(() => {
      banner.style.display = 'none'
    }, 300)
  }

  rememberDismissal(announcementId)
}

export function initializeAnnouncement(): void {
  const banner = document.getElementById('announcement-banner')
  if (!(banner instanceof HTMLElement)) {
    return
  }

  const announcementId = banner.dataset.announcementId ?? ''
  if (announcementId && !wasDismissed(announcementId)) {
    banner.style.display = 'block'
  }

  banner
    .querySelector('[data-dismiss-announcement]')
    ?.addEventListener('click', () => dismissAnnouncement(banner))
}
