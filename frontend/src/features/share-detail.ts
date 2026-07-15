import { copyText } from '../core/clipboard'
import { getCsrfToken } from '../core/csrf'
import { showMessage } from '../core/notify'
import { recordVisitHistory } from './visit-history'

type CounterField = 'copies_count' | 'views_count'

function readCounter(payload: unknown, field: CounterField): string | null {
  if (typeof payload !== 'object' || payload === null || !(field in payload)) {
    return null
  }
  const value = (payload as Record<string, unknown>)[field]
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

function updateCounter(
  root: HTMLElement,
  selector: string,
  payload: unknown,
  field: CounterField,
): void {
  const value = readCounter(payload, field)
  const counter = root.querySelector<HTMLElement>(selector)
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
    updateCounter(root, '[data-views-count]', payload, 'views_count')
  })
}

function recordCopy(root: HTMLElement): void {
  const url = root.dataset.recordCopyUrl
  if (!url) {
    return
  }
  void postCounter(url).then((payload) => {
    updateCounter(root, '[data-copies-count]', payload, 'copies_count')
  })
}

function showCopiedButton(button: HTMLButtonElement): void {
  const originalChildren = Array.from(button.childNodes, (node) => node.cloneNode(true))
  const icon = document.createElement('i')
  const label = document.createTextNode(' 已复制')
  icon.className = 'bi bi-check'
  button.replaceChildren(icon, label)
  button.classList.remove('btn-outline-secondary')
  button.classList.add('btn-success')
  button.disabled = true

  window.setTimeout(() => {
    button.replaceChildren(...originalChildren)
    button.classList.remove('btn-success')
    button.classList.add('btn-outline-secondary')
    button.disabled = false
  }, 2000)
}

function revealContent(root: HTMLElement, overlay: HTMLElement): void {
  if (overlay.dataset.revealing === 'true') {
    return
  }
  overlay.dataset.revealing = 'true'
  overlay.style.transition = 'opacity 0.3s'
  overlay.style.opacity = '0'

  window.setTimeout(() => {
    overlay.style.display = 'none'
    const hasVisibleOverlay = Array.from(
      root.querySelectorAll<HTMLElement>('[data-content-overlay]'),
    ).some((item) => item.style.display !== 'none')
    if (!hasVisibleOverlay) {
      root.querySelector<HTMLElement>('[data-share-preview]')?.classList.remove('blur-content')
    }
  }, 300)
}

function copyStrategyCode(root: HTMLElement, button: HTMLButtonElement): void {
  const code = root.querySelector<HTMLInputElement>('[data-share-code]')?.value
  if (code === undefined) {
    return
  }

  void copyText(code, () => {
    showMessage('代码已复制到剪贴板！', 'success')
    showCopiedButton(button)
    recordCopy(root)
  })
}

function copyShareUrl(root: HTMLElement, button: HTMLButtonElement): void {
  const url = root.dataset.shareUrl
  if (url === undefined) {
    return
  }

  void copyText(url, () => {
    showMessage('链接已复制到剪贴板！', 'success')
    showCopiedButton(button)
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

    const overlay = event.target.closest<HTMLElement>('[data-content-overlay]')
    if (overlay && root.contains(overlay)) {
      revealContent(root, overlay)
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
