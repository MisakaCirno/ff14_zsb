import { getCsrfToken } from '../core/csrf'
import { performShareCopy } from './share-copy'
import { recordVisitHistory } from './visit-history'

function readViewsCount(payload: unknown): string | null {
  if (typeof payload !== 'object' || payload === null || !('views_count' in payload)) {
    return null
  }
  const value = (payload as Record<string, unknown>).views_count
  return typeof value === 'number' || typeof value === 'string' ? String(value) : null
}

async function postCounter(url: string): Promise<unknown | null> {
  const csrfToken = getCsrfToken()
  if (!csrfToken) {
    return null
  }

  try {
    const response = await fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        Accept: 'application/json',
        'X-CSRFToken': csrfToken,
      },
    })
    return response.ok ? await response.json() as unknown : null
  } catch (error) {
    console.warn('Unable to update the share counter.', error)
    return null
  }
}

function updateViewsCounter(root: HTMLElement, payload: unknown): void {
  const value = readViewsCount(payload)
  const counter = root.querySelector<HTMLElement>('[data-views-count]')
  if (counter && value !== null) {
    counter.textContent = value
  }
}

function recordView(root: HTMLElement): void {
  const url = root.dataset.recordViewUrl
  if (!url) {
    return
  }
  void postCounter(url).then((payload) => {
    updateViewsCounter(root, payload)
  })
}

function revealContent(
  root: HTMLElement,
  button: HTMLButtonElement,
  overlay: HTMLElement,
): void {
  if (root.dataset.contentRevealed === 'true') {
    return
  }

  root.dataset.contentRevealed = 'true'
  button.setAttribute('aria-expanded', 'true')
  overlay.hidden = true

  const preview = root.querySelector<HTMLElement>('[data-share-preview]')
  if (preview) {
    preview.classList.remove('blur-content')
    preview.removeAttribute('aria-hidden')
    preview.tabIndex = -1
    preview.focus({ preventScroll: true })
  }

  root.dispatchEvent(new CustomEvent('share:content-revealed'))
}

function copyStrategyCode(root: HTMLElement, button: HTMLButtonElement): void {
  const code = root.querySelector<HTMLInputElement>('[data-share-code]')?.value
  if (code === undefined) {
    return
  }

  void performShareCopy({
    button,
    counterRoot: root,
    manualLabel: '战术板代码',
    recordUrl: root.dataset.recordCopyUrl,
    successLabel: '已复制',
    successMessage: '战术板代码已复制',
    text: code,
  })
}

function copyShareUrl(root: HTMLElement, button: HTMLButtonElement): void {
  const url = root.dataset.shareUrl
  if (url === undefined) {
    return
  }

  void performShareCopy({
    button,
    manualLabel: '分享链接',
    successLabel: '已复制',
    successMessage: '分享链接已复制',
    text: url,
  })
}

function initializeShareDetail(root: HTMLElement): void {
  const shareUrlInput = root.querySelector<HTMLInputElement>('[data-share-url-input]')
  const canonicalShareUrl = root.dataset.shareUrl
  if (shareUrlInput && canonicalShareUrl) {
    shareUrlInput.value = canonicalShareUrl
  }

  const shareId = root.dataset.shareId
  const shareTitle = root.dataset.shareTitle
  if (shareId && shareTitle !== undefined) {
    recordVisitHistory(shareId, shareTitle)
  }
  recordView(root)

  root.addEventListener('click', (event) => {
    if (!(event.target instanceof Element)) {
      return
    }

    const revealButton = event.target.closest<HTMLButtonElement>('[data-reveal-content]')
    const overlay = revealButton?.closest<HTMLElement>('[data-content-overlay]')
    if (revealButton && overlay && root.contains(overlay)) {
      revealContent(root, revealButton, overlay)
      return
    }

    const codeButton = event.target.closest<HTMLButtonElement>('[data-copy-detail-code]')
    if (codeButton && root.contains(codeButton)) {
      event.preventDefault()
      copyStrategyCode(root, codeButton)
      return
    }

    const urlButton = event.target.closest<HTMLButtonElement>('[data-copy-share-url]')
    if (urlButton && root.contains(urlButton)) {
      event.preventDefault()
      copyShareUrl(root, urlButton)
    }
  })
}

export function initializeShareDetails(): void {
  document.querySelectorAll<HTMLElement>('[data-share-detail]').forEach(initializeShareDetail)
}
