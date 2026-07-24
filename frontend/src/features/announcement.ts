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

function restoreFocus(target: HTMLElement): void {
  const focusTarget = target.isConnected && !target.closest('[hidden]')
    ? target
    : document.getElementById('main-content')
  focusTarget?.focus({ preventScroll: true })
}

function hideAnnouncementEntry(banner: HTMLElement): void {
  banner.hidden = true
}

function openAnnouncement(dialog: HTMLDialogElement): void {
  if (dialog.open) {
    return
  }
  if (typeof dialog.showModal === 'function') {
    dialog.showModal()
  } else {
    dialog.setAttribute('open', '')
  }
  dialog
    .querySelector<HTMLElement>('[data-announcement-dialog-close]')
    ?.focus({ preventScroll: true })
}

function dismissAnnouncement(
  dialog: HTMLDialogElement,
  announcementId: string,
  focusTarget: HTMLElement,
  banner: HTMLElement,
): void {
  if (dialog.open && typeof dialog.close === 'function') {
    dialog.close()
  } else {
    dialog.removeAttribute('open')
  }
  rememberDismissal(announcementId)
  hideAnnouncementEntry(banner)
  restoreFocus(focusTarget)
}

export function initializeAnnouncement(): void {
  const banner = document.querySelector<HTMLElement>('[data-announcement-banner]')
  const dialog = document.querySelector<HTMLDialogElement>('[data-announcement-dialog]')
  const trigger = document.querySelector<HTMLElement>('[data-announcement-open]')
  if (
    !banner
    || !dialog
    || !trigger
    || dialog.dataset.announcementInitialized === 'true'
  ) {
    return
  }
  dialog.dataset.announcementInitialized = 'true'

  const announcementId = dialog.dataset.announcementId ?? ''
  if (announcementId && wasDismissed(announcementId)) {
    hideAnnouncementEntry(banner)
    return
  }

  trigger.addEventListener('click', () => openAnnouncement(dialog))
  dialog.querySelectorAll<HTMLElement>('[data-dismiss-announcement]').forEach((button) => {
    button.addEventListener('click', () => {
      dismissAnnouncement(dialog, announcementId, trigger, banner)
    })
  })
  dialog.addEventListener('cancel', (event) => {
    event.preventDefault()
    dismissAnnouncement(dialog, announcementId, trigger, banner)
  })
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) {
      dismissAnnouncement(dialog, announcementId, trigger, banner)
    }
  })

  if (announcementId) {
    openAnnouncement(dialog)
  }
}
