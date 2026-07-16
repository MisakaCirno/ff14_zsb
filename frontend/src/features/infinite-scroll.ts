interface HtmxRequestDetail {
  elt?: Element
  requestConfig?: {
    elt?: Element
  }
  xhr?: object
}

const sentinelSelector = '[data-infinite-scroll-sentinel]'
const regionSelector = '[data-infinite-scroll-region]'
const cardSelector = '[data-share-card]'
const focusableSelector = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

interface KeyboardLoadPlan {
  existingCards: Set<HTMLElement>
  region: HTMLElement
  requestIdentity: object | null
  sentinel: HTMLButtonElement
}

const keyboardActivationCandidates = new WeakSet<HTMLButtonElement>()
let pendingKeyboardLoad: KeyboardLoadPlan | null = null

function getSentinel(event: Event): HTMLButtonElement | null {
  const detail = (event as CustomEvent<HtmxRequestDetail>).detail
  const eventElement = detail?.elt
  if (eventElement instanceof HTMLButtonElement && eventElement.matches(sentinelSelector)) {
    return eventElement
  }
  const requestElement = detail?.requestConfig?.elt
  if (requestElement instanceof HTMLButtonElement && requestElement.matches(sentinelSelector)) {
    return requestElement
  }
  return event.target instanceof HTMLButtonElement && event.target.matches(sentinelSelector)
    ? event.target
    : null
}

function requestIdentity(event: Event): object | null {
  const detail = (event as CustomEvent<HtmxRequestDetail>).detail
  return detail?.xhr ?? detail?.requestConfig ?? null
}

function rememberKeyboardActivation(event: KeyboardEvent): void {
  if (event.key !== 'Enter' && event.key !== ' ') {
    return
  }
  const sentinel = getSentinel(event)
  if (sentinel) {
    keyboardActivationCandidates.add(sentinel)
  }
}

function clearKeyboardActivationAfterKeyup(event: KeyboardEvent): void {
  const sentinel = getSentinel(event)
  if (!sentinel || (event.key !== 'Enter' && event.key !== ' ')) {
    return
  }

  // The native Space activation click runs as the keyup default action. Clear
  // an unused intent after that action has had a chance to start the request.
  queueMicrotask(() => keyboardActivationCandidates.delete(sentinel))
}

function clearKeyboardActivationFromPointer(event: PointerEvent): void {
  const sentinel = getSentinel(event)
  if (sentinel) {
    keyboardActivationCandidates.delete(sentinel)
  }
}

function prepareKeyboardLoad(event: Event, sentinel: HTMLButtonElement): void {
  if (!keyboardActivationCandidates.delete(sentinel)) {
    return
  }

  const region = sentinel.closest<HTMLElement>(regionSelector)
  if (!region) {
    return
  }

  pendingKeyboardLoad = {
    existingCards: new Set(region.querySelectorAll<HTMLElement>(cardSelector)),
    region,
    requestIdentity: requestIdentity(event),
    sentinel,
  }
}

function eventBelongsToPlan(event: Event, plan: KeyboardLoadPlan): boolean {
  const identity = requestIdentity(event)
  if (plan.requestIdentity && identity) {
    return plan.requestIdentity === identity
  }

  const detail = (event as CustomEvent<HtmxRequestDetail>).detail
  const requestElement = detail?.requestConfig?.elt
  if (requestElement) {
    return requestElement === plan.sentinel
  }
  return getSentinel(event) === plan.sentinel
    || (event.target instanceof Element && plan.region.contains(event.target))
}

function canRestoreFocus(plan: KeyboardLoadPlan): boolean {
  const activeElement = document.activeElement
  return activeElement === plan.sentinel
    || activeElement === document.body
    || activeElement === document.documentElement
}

function focusWithoutScrolling(element: HTMLElement): void {
  element.focus({ preventScroll: true })
}

function focusTargetForLoadedContent(plan: KeyboardLoadPlan): HTMLElement | null {
  const newCard = Array.from(
    plan.region.querySelectorAll<HTMLElement>(cardSelector),
  ).find(card => !plan.existingCards.has(card))
  if (newCard) {
    const interactiveTarget = newCard.querySelector<HTMLElement>(focusableSelector)
    if (interactiveTarget) {
      return interactiveTarget
    }
    newCard.tabIndex = -1
    return newCard
  }

  const nextSentinel = plan.region.querySelector<HTMLButtonElement>(sentinelSelector)
  if (nextSentinel && nextSentinel !== plan.sentinel) {
    return nextSentinel
  }
  return plan.region.querySelector<HTMLElement>('[data-infinite-scroll-end]')
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
  prepareKeyboardLoad(event, sentinel)
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

  const plan = pendingKeyboardLoad
  if (!plan || plan.sentinel !== sentinel || !eventBelongsToPlan(event, plan)) {
    return
  }
  pendingKeyboardLoad = null
  if (canRestoreFocus(plan)) {
    focusWithoutScrolling(sentinel)
  }
}

function restoreKeyboardFocusAfterSettle(event: Event): void {
  const plan = pendingKeyboardLoad
  if (!plan || !eventBelongsToPlan(event, plan)) {
    return
  }
  pendingKeyboardLoad = null
  if (!canRestoreFocus(plan)) {
    return
  }

  const target = focusTargetForLoadedContent(plan)
  if (target) {
    focusWithoutScrolling(target)
  }
}

export function initializeInfiniteScrollStatus(): void {
  document.addEventListener('keydown', rememberKeyboardActivation)
  document.addEventListener('keyup', clearKeyboardActivationAfterKeyup)
  document.addEventListener('pointerdown', clearKeyboardActivationFromPointer)
  document.addEventListener('htmx:beforeRequest', markLoading)
  document.addEventListener('htmx:responseError', markFailed)
  document.addEventListener('htmx:sendError', markFailed)
  document.addEventListener('htmx:timeout', markFailed)
  document.addEventListener('htmx:afterSettle', restoreKeyboardFocusAfterSettle)
}
