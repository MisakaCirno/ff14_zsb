import { showMessage } from '../core/notify'

type ShareInteractionKind = 'favorite' | 'like'
type ReactionTab = 'favorites' | 'likes'

interface HtmxFailureDetail {
  elt?: Element
  requestConfig?: {
    elt?: Element
  }
  xhr?: object
}

interface ReactionRemovedDetail {
  shareId?: unknown
}

interface PendingReactionFocus {
  buttonId: string
  key: string
  originCard: HTMLElement
  preferredButtonId: string | null
  tab: ReactionTab
}

const interactionSelector = '[data-share-interaction]'
const interactionFailureEventNames = [
  'htmx:responseError',
  'htmx:sendError',
  'htmx:timeout',
] as const
const failureMessages: Record<ShareInteractionKind, string> = {
  favorite: '收藏未完成，请稍后重试。',
  like: '点赞未完成，请稍后重试。',
}
const reactionRefreshFailureMessage = '互动状态已更新，但列表刷新失败，请刷新页面。'
const reactionTabs: Record<ShareInteractionKind, ReactionTab> = {
  favorite: 'favorites',
  like: 'likes',
}
const notifiedFailures = new WeakSet<object>()
const reactionFocusPlans = new Map<string, PendingReactionFocus>()

let initialized = false
let latestReactionFocusKey: string | null = null
let pendingReactionFocus: PendingReactionFocus | null = null

function interactionButtonFrom(element: Element | null | undefined): HTMLButtonElement | null {
  const button = element instanceof HTMLButtonElement
    ? element
    : element?.closest<HTMLButtonElement>(interactionSelector)
  if (!button || !button.matches(interactionSelector)) {
    return null
  }
  return button
}

function getInteractionButton(event: Event, detail: HtmxFailureDetail | undefined): HTMLButtonElement | null {
  const detailButton = interactionButtonFrom(detail?.elt)
  if (detailButton) {
    return detailButton
  }
  const requestButton = interactionButtonFrom(detail?.requestConfig?.elt)
  if (requestButton) {
    return requestButton
  }
  return interactionButtonFrom(event.target instanceof Element ? event.target : null)
}

function getInteractionKind(button: HTMLButtonElement): ShareInteractionKind | null {
  const kind = button.dataset.shareInteraction
  return kind === 'favorite' || kind === 'like' ? kind : null
}

function failureIdentity(event: Event, detail: HtmxFailureDetail | undefined): object {
  return detail?.xhr ?? detail ?? event
}

function getInteractionShareId(
  button: HTMLButtonElement,
  kind: ShareInteractionKind,
): string | null {
  const prefix = `btn-${kind}-`
  return button.id.startsWith(prefix) && button.id.length > prefix.length
    ? button.id.slice(prefix.length)
    : null
}

function reactionFocusKey(kind: ShareInteractionKind, shareId: string): string {
  return `${kind}:${shareId}`
}

function clearReactionFocusPlan(
  button: HTMLButtonElement,
  kind: ShareInteractionKind,
): void {
  const shareId = getInteractionShareId(button, kind)
  if (!shareId) {
    return
  }

  const key = reactionFocusKey(kind, shareId)
  reactionFocusPlans.delete(key)
  if (pendingReactionFocus?.key === key) {
    pendingReactionFocus = null
  }
  if (latestReactionFocusKey === key) {
    latestReactionFocusKey = null
  }
}

function reportInteractionFailure(event: Event): void {
  const detail = (event as CustomEvent<HtmxFailureDetail>).detail
  const button = getInteractionButton(event, detail)
  if (!button) {
    return
  }

  const kind = getInteractionKind(button)
  if (!kind) {
    return
  }

  clearReactionFocusPlan(button, kind)

  const identity = failureIdentity(event, detail)
  if (notifiedFailures.has(identity)) {
    return
  }
  notifiedFailures.add(identity)
  showMessage(failureMessages[kind], 'danger')
}

function reactionSectionFrom(element: Element | null | undefined): HTMLElement | null {
  if (
    !(element instanceof HTMLElement)
    || !element.matches('[data-my-content-shares]')
  ) {
    return null
  }
  const tab = element.dataset.myContentSection
  return tab === 'likes' || tab === 'favorites' ? element : null
}

function reportReactionRefreshFailure(event: Event): void {
  const detail = (event as CustomEvent<HtmxFailureDetail>).detail
  const section = reactionSectionFrom(
    detail?.requestConfig?.elt
      ?? detail?.elt
      ?? (event.target instanceof Element ? event.target : null),
  )
  if (!section) {
    return
  }

  const pending = pendingReactionFocus
  if (pending && pending.tab === section.dataset.myContentSection) {
    if (latestReactionFocusKey === pending.key) {
      latestReactionFocusKey = null
    }
    pendingReactionFocus = null
  }

  const identity = failureIdentity(event, detail)
  if (notifiedFailures.has(identity)) {
    return
  }
  notifiedFailures.add(identity)
  showMessage(reactionRefreshFailureMessage, 'danger')
}

function findReactionSection(tab: ReactionTab): HTMLElement | null {
  return document.querySelector<HTMLElement>(
    `[data-my-content-shares][data-my-content-section="${tab}"]`,
  )
}

function findPreferredNeighborButtonId(
  section: HTMLElement,
  currentButton: HTMLButtonElement,
  kind: ShareInteractionKind,
): string | null {
  const currentCard = currentButton.closest<HTMLElement>('[data-share-card]')
  if (!currentCard) {
    return null
  }

  const cards = Array.from(
    section.querySelectorAll<HTMLElement>('[data-share-card]'),
  )
  const currentIndex = cards.indexOf(currentCard)
  if (currentIndex < 0) {
    return null
  }

  for (const neighbor of [cards[currentIndex + 1], cards[currentIndex - 1]]) {
    const candidate = neighbor?.querySelector<HTMLButtonElement>(
      `[data-share-interaction="${kind}"]`,
    )
    if (candidate?.id) {
      return candidate.id
    }
  }
  return null
}

function prepareReactionRemovalFocus(
  kind: ShareInteractionKind,
  button: HTMLButtonElement,
): void {
  const tab = reactionTabs[kind]
  const section = findReactionSection(tab)
  const originCard = button.closest<HTMLElement>('[data-share-card]')
  const activeElement = document.activeElement
  const shareId = getInteractionShareId(button, kind)
  if (
    !section
    || !originCard
    || !shareId
    || !section.contains(button)
    || getInteractionKind(button) !== kind
    || (activeElement !== button && !originCard.contains(activeElement))
  ) {
    return
  }

  const key = reactionFocusKey(kind, shareId)
  reactionFocusPlans.set(key, {
    buttonId: button.id,
    key,
    originCard,
    preferredButtonId: findPreferredNeighborButtonId(section, button, kind),
    tab,
  })
  latestReactionFocusKey = key
}

function prepareReactionFocusBeforeRequest(event: Event): void {
  const detail = (event as CustomEvent<HtmxFailureDetail>).detail
  const button = getInteractionButton(event, detail)
  if (!button || button.value !== 'inactive') {
    return
  }

  const kind = getInteractionKind(button)
  if (kind) {
    prepareReactionRemovalFocus(kind, button)
  }
}

function activateReactionFocusPlan(
  kind: ShareInteractionKind,
  event: Event,
): void {
  const detail = (event as CustomEvent<ReactionRemovedDetail>).detail
  const shareId = typeof detail?.shareId === 'string'
    || typeof detail?.shareId === 'number'
    ? String(detail.shareId)
    : ''
  if (!shareId) {
    return
  }

  const key = reactionFocusKey(kind, shareId)
  const plan = reactionFocusPlans.get(key)
  reactionFocusPlans.delete(key)
  if (!plan || latestReactionFocusKey !== key) {
    return
  }

  const activeElement = document.activeElement
  if (
    plan.originCard.isConnected
    && activeElement !== document.body
    && !plan.originCard.contains(activeElement)
  ) {
    latestReactionFocusKey = null
    return
  }
  pendingReactionFocus = plan
}

function completeInteractionRequest(event: Event): void {
  const detail = (event as CustomEvent<HtmxFailureDetail>).detail
  const button = getInteractionButton(event, detail)
  if (!button) {
    return
  }

  const kind = getInteractionKind(button)
  const shareId = kind ? getInteractionShareId(button, kind) : null
  if (!kind || !shareId) {
    return
  }

  const key = reactionFocusKey(kind, shareId)
  reactionFocusPlans.delete(key)
  if (latestReactionFocusKey === key && pendingReactionFocus?.key !== key) {
    latestReactionFocusKey = null
  }
}

function observeInteractionRequestLifecycle(event: Event): void {
  const detail = (event as CustomEvent<HtmxFailureDetail>).detail
  const button = getInteractionButton(event, detail)
  if (!button) {
    return
  }

  const requestIdentity = failureIdentity(event, detail)
  let cleanupScheduled = false
  const belongsToObservedRequest = (requestEvent: Event): boolean => {
    const requestDetail = (requestEvent as CustomEvent<HtmxFailureDetail>).detail
    return failureIdentity(requestEvent, requestDetail) === requestIdentity
  }
  const reportDirectFailure = (requestEvent: Event): void => {
    if (!belongsToObservedRequest(requestEvent)) {
      return
    }
    reportInteractionFailure(requestEvent)
  }
  const cleanup = (): void => {
    for (const eventName of interactionFailureEventNames) {
      button.removeEventListener(eventName, reportDirectFailure)
    }
    button.removeEventListener('htmx:afterRequest', completeDirectRequest)
  }
  const completeDirectRequest = (requestEvent: Event): void => {
    if (!belongsToObservedRequest(requestEvent)) {
      return
    }
    completeInteractionRequest(requestEvent)
    if (cleanupScheduled) {
      return
    }
    cleanupScheduled = true

    // HTMX dispatches afterRequest before sendError/timeout. Keep this request's
    // direct listeners alive until those synchronous terminal events finish.
    queueMicrotask(cleanup)
  }

  for (const eventName of interactionFailureEventNames) {
    button.addEventListener(eventName, reportDirectFailure)
  }
  button.addEventListener('htmx:afterRequest', completeDirectRequest)
}

function cancelFocusRestoreAfterUserMove(event: Event): void {
  if (!latestReactionFocusKey || !(event.target instanceof HTMLElement)) {
    return
  }

  const plan = pendingReactionFocus?.key === latestReactionFocusKey
    ? pendingReactionFocus
    : reactionFocusPlans.get(latestReactionFocusKey)
  if (!plan) {
    latestReactionFocusKey = null
    return
  }
  if (plan.originCard.contains(event.target) || event.target.id === plan.buttonId) {
    return
  }

  latestReactionFocusKey = null
  if (pendingReactionFocus?.key === plan.key) {
    pendingReactionFocus = null
  }
}

function focusAfterBrowserSettles(callback: () => void): void {
  if (typeof window.requestAnimationFrame === 'function') {
    window.requestAnimationFrame(callback)
  } else {
    window.setTimeout(callback, 0)
  }
}

function restoreReactionFocusAfterRefresh(event: Event): void {
  const pending = pendingReactionFocus
  if (
    !pending
    || !(event.target instanceof HTMLElement)
    || !event.target.matches('[data-my-content-shares]')
    || event.target.dataset.myContentSection !== pending.tab
  ) {
    return
  }

  pendingReactionFocus = null
  if (latestReactionFocusKey === pending.key) {
    latestReactionFocusKey = null
  }
  const section = event.target
  focusAfterBrowserSettles(() => {
    const activeElement = document.activeElement
    if (
      activeElement !== document.body
      && activeElement !== document.documentElement
      && activeElement !== section
    ) {
      return
    }

    const preferredButton = pending.preferredButtonId
      ? document.getElementById(pending.preferredButtonId)
      : null
    if (preferredButton instanceof HTMLElement && section.contains(preferredButton)) {
      preferredButton.focus({ preventScroll: true })
      return
    }

    section.querySelector<HTMLElement>('#my-content-section-title')
      ?.focus({ preventScroll: true })
  })
}

export function initializeShareInteractions(): void {
  if (initialized) {
    return
  }
  initialized = true

  for (const eventName of interactionFailureEventNames) {
    document.addEventListener(eventName, reportInteractionFailure)
    document.addEventListener(eventName, reportReactionRefreshFailure)
  }
  document.addEventListener('htmx:beforeRequest', prepareReactionFocusBeforeRequest)
  document.addEventListener('htmx:beforeSend', observeInteractionRequestLifecycle)
  document.addEventListener('htmx:afterRequest', completeInteractionRequest)
  document.addEventListener('share-like-removed', (event) => {
    activateReactionFocusPlan('like', event)
  }, true)
  document.addEventListener('share-favorite-removed', (event) => {
    activateReactionFocusPlan('favorite', event)
  }, true)
  document.addEventListener('htmx:afterSettle', restoreReactionFocusAfterRefresh)
  document.addEventListener('focusin', cancelFocusRestoreAfterUserMove)
}
