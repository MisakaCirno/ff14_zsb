interface HtmxRequestDetail {
  elt?: Element
}

const sentinelSelector = '[data-infinite-scroll-sentinel]'

function getSentinel(event: Event): HTMLButtonElement | null {
  const eventElement = (event as CustomEvent<HtmxRequestDetail>).detail?.elt
  if (eventElement instanceof HTMLButtonElement && eventElement.matches(sentinelSelector)) {
    return eventElement
  }
  return event.target instanceof HTMLButtonElement && event.target.matches(sentinelSelector)
    ? event.target
    : null
}

function setMessage(sentinel: HTMLButtonElement, message: string): void {
  const messageElement = sentinel.querySelector('[data-infinite-scroll-message]')
  if (messageElement) {
    messageElement.textContent = message
  }
}

function markLoading(event: Event): void {
  const sentinel = getSentinel(event)
  if (!sentinel) {
    return
  }
  sentinel.disabled = true
  sentinel.setAttribute('aria-busy', 'true')
  setMessage(sentinel, '正在加载更多...')
}

function markFailed(event: Event): void {
  const sentinel = getSentinel(event)
  if (!sentinel) {
    return
  }
  sentinel.disabled = false
  sentinel.removeAttribute('aria-busy')
  setMessage(sentinel, '加载失败，点击重试')
}

export function initializeInfiniteScrollStatus(): void {
  document.addEventListener('htmx:beforeRequest', markLoading)
  document.addEventListener('htmx:responseError', markFailed)
  document.addEventListener('htmx:sendError', markFailed)
  document.addEventListener('htmx:timeout', markFailed)
}
