import { copyText, type CopyTextResult } from '../core/clipboard'
import { getCsrfToken } from '../core/csrf'
import { showMessage } from '../core/notify'
import { showManualCopy } from './manual-copy'

export interface ActionButtonSnapshot {
  ariaBusy: string | null
  ariaLabel: string | null
  ariaLive: string | null
  children: Node[]
  className: string
  disabled: boolean
  hadFocus: boolean
}

export interface ShareCopyOptions {
  button: HTMLButtonElement
  compactFeedback?: boolean
  counterRoot?: ParentNode | null
  manualLabel: string
  recordUrl?: string | undefined
  successLabel?: string
  successMessage: string
  text: string
}

function restoreAttribute(element: Element, name: string, value: string | null): void {
  if (value === null) {
    element.removeAttribute(name)
  } else {
    element.setAttribute(name, value)
  }
}

export function snapshotActionButton(button: HTMLButtonElement): ActionButtonSnapshot {
  return {
    ariaBusy: button.getAttribute('aria-busy'),
    ariaLabel: button.getAttribute('aria-label'),
    ariaLive: button.getAttribute('aria-live'),
    children: Array.from(button.childNodes, (node) => node.cloneNode(true)),
    className: button.className,
    disabled: button.disabled,
    hadFocus: document.activeElement === button,
  }
}

export function restoreActionButton(
  button: HTMLButtonElement,
  snapshot: ActionButtonSnapshot,
): void {
  button.replaceChildren(...snapshot.children)
  button.className = snapshot.className
  button.disabled = snapshot.disabled
  restoreAttribute(button, 'aria-busy', snapshot.ariaBusy)
  restoreAttribute(button, 'aria-label', snapshot.ariaLabel)
  restoreAttribute(button, 'aria-live', snapshot.ariaLive)
  if (snapshot.hadFocus && document.activeElement === document.body) {
    button.focus({ preventScroll: true })
  }
}

export function setActionButtonLabel(
  button: HTMLButtonElement,
  iconClass: string,
  label: string,
  compact = false,
): void {
  const icon = document.createElement('i')
  icon.className = iconClass
  icon.setAttribute('aria-hidden', 'true')
  if (compact) {
    const hiddenLabel = document.createElement('span')
    hiddenLabel.className = 'visually-hidden'
    hiddenLabel.textContent = label
    button.replaceChildren(icon, hiddenLabel)
  } else {
    button.replaceChildren(icon, document.createTextNode(` ${label}`))
  }
  button.setAttribute('aria-label', label)
}

export function setActionButtonBusy(
  button: HTMLButtonElement,
  label: string,
  iconClass = 'bi bi-hourglass-split',
): void {
  button.disabled = true
  button.setAttribute('aria-busy', 'true')
  button.setAttribute('aria-live', 'polite')
  setActionButtonLabel(button, iconClass, label)
}

export function setActionButtonSuccess(
  button: HTMLButtonElement,
  label: string,
  compact = false,
): void {
  button.disabled = true
  button.setAttribute('aria-busy', 'false')
  button.setAttribute('aria-live', 'polite')
  button.classList.remove(
    'btn-primary',
    'btn-outline-primary',
    'btn-outline-secondary',
  )
  button.classList.add('btn-success')
  setActionButtonLabel(button, 'bi bi-check-circle', label, compact)
}

export function scheduleActionButtonRestore(
  button: HTMLButtonElement,
  snapshot: ActionButtonSnapshot,
  delay = 2000,
): void {
  window.setTimeout(() => restoreActionButton(button, snapshot), delay)
}

function readCopiesCount(payload: unknown): string | null {
  if (typeof payload !== 'object' || payload === null || !('copies_count' in payload)) {
    return null
  }
  const value = (payload as Record<string, unknown>).copies_count
  return typeof value === 'number' || typeof value === 'string' ? String(value) : null
}

export async function recordShareCopy(url: string): Promise<string | null> {
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
    if (!response.ok) {
      return null
    }
    return readCopiesCount(await response.json() as unknown)
  } catch (error) {
    console.warn('Unable to update the share copy counter.', error)
    return null
  }
}

function updateCopyCounters(root: ParentNode | null, value: string | null): void {
  if (!root || value === null) {
    return
  }
  root.querySelectorAll<HTMLElement>('[data-copies-count]').forEach((counter) => {
    counter.textContent = value
  })
  root.querySelectorAll<HTMLButtonElement>(
    '[data-copy-strategy][data-copy-label]',
  ).forEach((button) => {
    button.setAttribute(
      'aria-label',
      `${button.dataset.copyLabel}，当前已复制 ${value} 次`,
    )
  })
}

function updateSnapshotCopyCounters(
  snapshot: ActionButtonSnapshot,
  value: string | null,
): void {
  if (value === null) {
    return
  }
  snapshot.children.forEach((node) => {
    if (!(node instanceof Element)) {
      return
    }
    if (node.matches('[data-copies-count]')) {
      node.textContent = value
    }
    node.querySelectorAll<HTMLElement>('[data-copies-count]').forEach((counter) => {
      counter.textContent = value
    })
  })
}

function updateSnapshotCopyLabel(
  button: HTMLButtonElement,
  snapshot: ActionButtonSnapshot,
  value: string | null,
): void {
  if (value === null || !button.dataset.copyLabel) {
    return
  }
  snapshot.ariaLabel = (
    `${button.dataset.copyLabel}，当前已复制 ${value} 次`
  )
}

export async function performShareCopy(options: ShareCopyOptions): Promise<CopyTextResult> {
  const snapshot = snapshotActionButton(options.button)
  setActionButtonBusy(options.button, '复制中...')
  const result = await copyText(options.text)

  if (result.status === 'manual-required') {
    restoreActionButton(options.button, snapshot)
    options.button.focus({ preventScroll: true })
    showManualCopy(options.text, options.manualLabel)
    return result
  }

  showMessage(options.successMessage, 'success')
  setActionButtonSuccess(
    options.button,
    options.successLabel ?? '已复制',
    options.compactFeedback ?? false,
  )
  scheduleActionButtonRestore(options.button, snapshot)

  if (options.recordUrl) {
    const fallbackRoot = options.button.closest<HTMLElement>(
      '[data-share-detail], [data-share-card]',
    )
    void recordShareCopy(options.recordUrl).then((value) => {
      updateCopyCounters(options.counterRoot ?? fallbackRoot, value)
      updateSnapshotCopyCounters(snapshot, value)
      updateSnapshotCopyLabel(options.button, snapshot, value)
    })
  }
  return result
}
