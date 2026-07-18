import { initializeShareDetails } from './share-detail'

interface ShareDetailHistoryState {
  shareDetailOverlay: true
  shareUrl: string
}

const dialogSelector = '[data-share-detail-dialog]'
const contentSelector = '[data-share-detail-dialog-content]'
const triggerSelector = '[data-share-detail-trigger]'
const loadingMarkup = `
  <div class="app-share-dialog__loading" role="status">
    <span class="spinner-border" aria-hidden="true"></span>
    <span>正在载入详情…</span>
  </div>
`

let initialized = false
let activeRequest: AbortController | null = null
let returnFocusTarget: HTMLElement | null = null

function isOverlayHistoryState(value: unknown): value is ShareDetailHistoryState {
  return typeof value === 'object'
    && value !== null
    && 'shareDetailOverlay' in value
    && value.shareDetailOverlay === true
    && 'shareUrl' in value
    && typeof value.shareUrl === 'string'
}

function dialogElements(): {
  content: HTMLElement
  dialog: HTMLDialogElement
} | null {
  const dialog = document.querySelector<HTMLDialogElement>(dialogSelector)
  const content = dialog?.querySelector<HTMLElement>(contentSelector)
  return dialog && content ? { content, dialog } : null
}

function navigateToFullDetail(url: string): void {
  window.location.assign(url)
}

function restoreTriggerFocus(): void {
  if (returnFocusTarget?.isConnected) {
    returnFocusTarget.focus({ preventScroll: true })
  }
  returnFocusTarget = null
}

function clearDialog(dialog: HTMLDialogElement, content: HTMLElement): void {
  activeRequest?.abort()
  activeRequest = null
  if (dialog.open) {
    dialog.close()
  }
  content.innerHTML = loadingMarkup
}

function closeDialog(restoreHistory: boolean): void {
  const elements = dialogElements()
  if (!elements) {
    return
  }

  const shouldGoBack = restoreHistory && isOverlayHistoryState(window.history.state)
  clearDialog(elements.dialog, elements.content)
  restoreTriggerFocus()
  if (shouldGoBack) {
    window.history.back()
  }
}

function overlayRequestUrl(shareUrl: string): string {
  const url = new URL(shareUrl, window.location.href)
  url.searchParams.set('presentation', 'overlay')
  return url.toString()
}

function canEnhanceNavigation(event: MouseEvent, link: HTMLAnchorElement): boolean {
  if (
    event.defaultPrevented
    || event.button !== 0
    || event.altKey
    || event.ctrlKey
    || event.metaKey
    || event.shiftKey
    || link.target
    || link.hasAttribute('download')
  ) {
    return false
  }
  return new URL(link.href, window.location.href).origin === window.location.origin
}

function showDialog(dialog: HTMLDialogElement): boolean {
  if (typeof dialog.showModal !== 'function') {
    return false
  }
  if (dialog.dataset.shareDetailCancelReady !== 'true') {
    dialog.dataset.shareDetailCancelReady = 'true'
    dialog.addEventListener('cancel', handleDialogCancel)
  }
  if (!dialog.open) {
    dialog.showModal()
  }
  return true
}

async function openOverlay(shareUrl: string, pushHistory: boolean): Promise<void> {
  const elements = dialogElements()
  if (!elements || !showDialog(elements.dialog)) {
    navigateToFullDetail(shareUrl)
    return
  }

  activeRequest?.abort()
  const request = new AbortController()
  activeRequest = request
  elements.content.innerHTML = loadingMarkup

  try {
    const response = await fetch(overlayRequestUrl(shareUrl), {
      credentials: 'same-origin',
      headers: {
        Accept: 'text/html',
        'HX-Request': 'true',
      },
      signal: request.signal,
    })
    if (!response.ok || response.redirected) {
      clearDialog(elements.dialog, elements.content)
      navigateToFullDetail(response.redirected ? response.url : shareUrl)
      return
    }

    elements.content.innerHTML = await response.text()
    window.htmx?.process(elements.content)
    initializeShareDetails()
    activeRequest = null

    const canonicalUrl = new URL(shareUrl, window.location.href).toString()
    if (pushHistory) {
      window.history.pushState({
        shareDetailOverlay: true,
        shareUrl: canonicalUrl,
      } satisfies ShareDetailHistoryState, '', canonicalUrl)
    }
    elements.content.querySelector<HTMLElement>('[data-share-detail-dialog-close]')
      ?.focus({ preventScroll: true })
  } catch (error) {
    if (request.signal.aborted) {
      return
    }
    console.warn('Unable to open the share detail overlay.', error)
    clearDialog(elements.dialog, elements.content)
    navigateToFullDetail(shareUrl)
  }
}

function syncReactionButton(source: HTMLButtonElement, target: HTMLButtonElement): void {
  const kind = source.dataset.shareInteraction
  const active = source.getAttribute('aria-pressed') === 'true'
  const sourceCount = source.querySelector<HTMLElement>('[data-reaction-count]')
  const targetCount = target.querySelector<HTMLElement>('[data-reaction-count]')

  target.setAttribute('aria-pressed', String(active))
  target.setAttribute('aria-label', source.getAttribute('aria-label') ?? '')
  target.setAttribute('hx-vals', source.getAttribute('hx-vals') ?? '')
  target.value = source.value
  target.title = source.title
  if (sourceCount && targetCount) {
    targetCount.textContent = sourceCount.textContent
  }

  const icon = target.querySelector<HTMLElement>('i')
  if (kind === 'like') {
    target.classList.toggle('btn-danger', active)
    target.classList.toggle('btn-outline-danger', !active)
    icon?.classList.toggle('bi-heart-fill', active)
    icon?.classList.toggle('bi-heart', !active)
  } else if (kind === 'favorite') {
    target.classList.toggle('btn-warning', active)
    target.classList.toggle('btn-outline-secondary', !active)
    icon?.classList.toggle('bi-star-fill', active)
    icon?.classList.toggle('bi-star', !active)
  }
}

function syncOverlayReactions(): void {
  const elements = dialogElements()
  const root = elements?.content.querySelector<HTMLElement>('[data-share-detail-overlay]')
  const shareId = root?.dataset.shareId
  if (!elements || !root || !shareId) {
    return
  }

  const matchingCards = Array.from(
    document.querySelectorAll<HTMLElement>('[data-share-card][data-share-id]'),
  ).filter(card => card.dataset.shareId === shareId)
  root.querySelectorAll<HTMLButtonElement>('[data-share-interaction]').forEach((source) => {
    const kind = source.dataset.shareInteraction
    for (const card of matchingCards) {
      const target = card.querySelector<HTMLButtonElement>(
        `[data-share-interaction="${kind}"]`,
      )
      if (target) {
        syncReactionButton(source, target)
      }
    }
  })
}

function handleDocumentClick(event: MouseEvent): void {
  if (!(event.target instanceof Element)) {
    return
  }

  const closeButton = event.target.closest<HTMLElement>('[data-share-detail-dialog-close]')
  if (closeButton) {
    event.preventDefault()
    syncOverlayReactions()
    closeDialog(true)
    return
  }

  const link = event.target.closest<HTMLAnchorElement>(triggerSelector)
  if (!link || !canEnhanceNavigation(event, link)) {
    return
  }
  event.preventDefault()
  returnFocusTarget = link
  void openOverlay(link.href, true)
}

function handleHistoryChange(event: PopStateEvent): void {
  if (isOverlayHistoryState(event.state)) {
    void openOverlay(event.state.shareUrl, false)
    return
  }
  syncOverlayReactions()
  closeDialog(false)
}

function handleDialogCancel(event: Event): void {
  if (!(event.target instanceof HTMLDialogElement) || !event.target.matches(dialogSelector)) {
    return
  }
  event.preventDefault()
  syncOverlayReactions()
  closeDialog(true)
}

function handleHtmxSettle(event: Event): void {
  const elements = dialogElements()
  if (
    elements
    && event.target instanceof Node
    && elements.content.contains(event.target)
  ) {
    syncOverlayReactions()
  }
}

export function initializeShareDetailDialog(): void {
  if (initialized) {
    return
  }
  initialized = true
  document.addEventListener('click', handleDocumentClick)
  document.addEventListener('htmx:afterSettle', handleHtmxSettle)
  window.addEventListener('popstate', handleHistoryChange)
}
