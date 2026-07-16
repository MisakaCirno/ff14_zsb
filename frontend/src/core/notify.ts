const allowedTypes = new Set([
  'danger',
  'info',
  'primary',
  'secondary',
  'success',
  'warning',
])

const persistentTypes = new Set(['danger', 'warning'])
const autoDismissDelay = 3000
const removalDelay = 150

function focusAfterRemoval(preferredTarget: HTMLElement | null): void {
  if (preferredTarget?.isConnected) {
    preferredTarget.focus({ preventScroll: true })
    if (document.activeElement === preferredTarget) {
      return
    }
  }

  const main = document.getElementById('main-content')
  if (main instanceof HTMLElement) {
    main.focus({ preventScroll: true })
  }
}

export function showMessage(message: unknown, type = 'success'): void {
  const container = document.getElementById('message-container')
  if (!container) {
    return
  }

  const safeType = allowedTypes.has(type) ? type : 'info'
  const alert = document.createElement('div')
  const messageText = document.createElement('span')
  const closeButton = document.createElement('button')
  const activeElement = document.activeElement
  const returnFocusTarget = activeElement instanceof HTMLElement
    && !container.contains(activeElement)
    && activeElement !== document.body
    && activeElement !== document.documentElement
    ? activeElement
    : null
  let autoDismissPending = false
  let dismissStarted = false

  alert.className = `app-notification alert alert-${safeType} alert-dismissible fade show shadow`
  alert.role = 'alert'
  alert.dataset.notification = ''
  if (persistentTypes.has(safeType)) {
    alert.dataset.notificationPersistent = ''
  }
  messageText.className = 'app-notification__message'
  messageText.textContent = String(message ?? '')
  closeButton.type = 'button'
  closeButton.className = 'btn-close'
  closeButton.setAttribute('aria-label', '关闭通知')

  const dismiss = (source: 'auto' | 'manual'): void => {
    if (dismissStarted) {
      return
    }
    if (source === 'auto' && alert.contains(document.activeElement)) {
      autoDismissPending = true
      return
    }

    dismissStarted = true
    const restoreFocus = source === 'manual'
      && alert.contains(document.activeElement)
    alert.classList.remove('show')
    window.setTimeout(() => {
      if (source === 'auto' && alert.contains(document.activeElement)) {
        dismissStarted = false
        autoDismissPending = true
        alert.classList.add('show')
        return
      }

      const shouldRestoreFocus = restoreFocus
        && alert.contains(document.activeElement)
      alert.remove()
      if (shouldRestoreFocus) {
        focusAfterRemoval(returnFocusTarget)
      }
    }, removalDelay)
  }

  closeButton.addEventListener('click', () => dismiss('manual'))
  alert.addEventListener('focusout', () => {
    if (!autoDismissPending) {
      return
    }
    queueMicrotask(() => {
      if (!alert.contains(document.activeElement)) {
        autoDismissPending = false
        dismiss('auto')
      }
    })
  })
  alert.append(messageText, closeButton)
  container.appendChild(alert)
  if (!persistentTypes.has(safeType)) {
    window.setTimeout(() => dismiss('auto'), autoDismissDelay)
  }
}
