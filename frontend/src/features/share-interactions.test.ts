// @vitest-environment jsdom

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { initializeShareInteractions } from './share-interactions'

type InteractionKind = 'favorite' | 'like'
type ReactionTab = 'favorites' | 'likes'

const requestXhrs = new WeakMap<HTMLButtonElement, object>()

function createInteractionButton(kind: InteractionKind, shareId = 'share-1'): HTMLButtonElement {
  const button = document.createElement('button')
  button.id = `btn-${kind}-${shareId}`
  button.dataset.shareInteraction = kind
  button.setAttribute('aria-pressed', 'true')
  document.body.appendChild(button)
  return button
}

function dispatchFailure(
  button: HTMLButtonElement,
  eventName: string,
  xhr: object,
): void {
  button.dispatchEvent(new CustomEvent(eventName, {
    bubbles: true,
    detail: { elt: button, requestConfig: { elt: button }, xhr },
  }))
}

function dispatchBeforeSend(button: HTMLButtonElement, xhr: object = {}): void {
  button.dispatchEvent(new CustomEvent('htmx:beforeSend', {
    bubbles: true,
    detail: { elt: button, requestConfig: { elt: button }, xhr },
  }))
}

function createReactionSection(
  tab: ReactionTab,
  kind: InteractionKind,
  shareIds: string[],
): HTMLElement {
  const section = document.createElement('section')
  section.dataset.myContentShares = ''
  section.dataset.myContentSection = tab

  const heading = document.createElement('h2')
  heading.id = 'my-content-section-title'
  heading.tabIndex = -1
  heading.textContent = '我的互动'
  section.appendChild(heading)

  for (const shareId of shareIds) {
    const card = document.createElement('article')
    card.dataset.shareCard = ''
    const button = document.createElement('button')
    button.id = `btn-${kind}-${shareId}`
    button.dataset.shareInteraction = kind
    button.value = 'inactive'
    card.appendChild(button)
    section.appendChild(card)
  }
  return section
}

function startRemovalRequest(
  button: HTMLButtonElement,
  focus = true,
  xhr: object = {},
): void {
  if (focus) {
    button.focus()
  }
  button.dispatchEvent(new CustomEvent('htmx:beforeRequest', {
    bubbles: true,
    detail: { elt: button, requestConfig: { elt: button }, xhr },
  }))
  requestXhrs.set(button, xhr)
  dispatchBeforeSend(button, xhr)
}

function dispatchRemoval(kind: InteractionKind, shareId: string): void {
  document.body.dispatchEvent(new CustomEvent(`share-${kind}-removed`, {
    bubbles: true,
    detail: { shareId },
  }))
}

function finishInteractionRequest(
  button: HTMLButtonElement,
  xhr: object = requestXhrs.get(button) ?? {},
): void {
  button.dispatchEvent(new CustomEvent('htmx:afterRequest', {
    bubbles: true,
    detail: { elt: button, requestConfig: { elt: button }, xhr },
  }))
}

function dispatchSectionFailure(
  section: HTMLElement,
  eventName: string,
  xhr: object,
): void {
  section.dispatchEvent(new CustomEvent(eventName, {
    bubbles: true,
    detail: { elt: section, requestConfig: { elt: section }, xhr },
  }))
}

function finishSectionSettle(section: HTMLElement): void {
  section.dispatchEvent(new CustomEvent('htmx:afterSettle', {
    bubbles: true,
    detail: { target: section },
  }))
  vi.advanceTimersByTime(20)
}

describe('share interactions', () => {
  beforeAll(() => {
    initializeShareInteractions()
  })

  beforeEach(() => {
    vi.useFakeTimers()
    document.body.innerHTML = `
      <div id="message-container" role="region" aria-live="polite"></div>
    `
  })

  afterEach(() => {
    vi.clearAllTimers()
    vi.useRealTimers()
  })

  it('shows one fixed danger message for duplicate failure events from one request', () => {
    const button = createInteractionButton('like')
    const xhr = { responseText: 'server secret must not be shown' }

    dispatchBeforeSend(button, xhr)
    dispatchFailure(button, 'htmx:responseError', xhr)
    dispatchFailure(button, 'htmx:timeout', xhr)
    finishInteractionRequest(button, xhr)

    const notifications = document.querySelectorAll('[data-notification]')
    expect(notifications).toHaveLength(1)
    expect(notifications[0]?.classList.contains('alert-danger')).toBe(true)
    expect(notifications[0]?.textContent).toContain('点赞未完成，请稍后重试。')
    expect(notifications[0]?.textContent).not.toContain('server secret')
    expect(button.getAttribute('aria-pressed')).toBe('true')
  })

  it('keeps request-scoped listeners isolated by xhr identity', async () => {
    const button = createInteractionButton('favorite')
    const firstXhr = {}
    const secondXhr = {}
    dispatchBeforeSend(button, firstXhr)
    dispatchBeforeSend(button, secondXhr)

    finishInteractionRequest(button, firstXhr)
    await Promise.resolve()
    button.remove()

    finishInteractionRequest(button, secondXhr)
    dispatchFailure(button, 'htmx:sendError', secondXhr)

    const notifications = document.querySelectorAll('[data-notification]')
    expect(notifications).toHaveLength(1)
    expect(notifications[0]?.textContent).toContain('收藏未完成，请稍后重试。')
  })

  it('uses the favorite message for a send error and ignores unrelated requests', () => {
    const favoriteButton = createInteractionButton('favorite')
    dispatchFailure(favoriteButton, 'htmx:sendError', {})

    const unrelated = document.createElement('button')
    document.body.appendChild(unrelated)
    dispatchFailure(unrelated, 'htmx:responseError', {})

    const notifications = document.querySelectorAll('[data-notification]')
    expect(notifications).toHaveLength(1)
    expect(notifications[0]?.textContent).toContain('收藏未完成，请稍后重试。')
  })

  it('reports a timeout when it is the only failure event', () => {
    const button = createInteractionButton('like')

    dispatchFailure(button, 'htmx:timeout', {})

    const notification = document.querySelector('[data-notification]')
    expect(notification?.textContent).toContain('点赞未完成，请稍后重试。')
  })

  it.each([
    ['like', 'likes'],
    ['favorite', 'favorites'],
  ] as const)(
    'restores focus to the next %s card after the authoritative %s refresh',
    (kind, tab) => {
      const oldSection = createReactionSection(tab, kind, ['a', 'b', 'c'])
      document.body.appendChild(oldSection)
      const button = oldSection.querySelector<HTMLButtonElement>(`#btn-${kind}-b`)!
      startRemovalRequest(button)
      dispatchRemoval(kind, 'b')
      finishInteractionRequest(button)

      const replacement = createReactionSection(tab, kind, ['a', 'c'])
      oldSection.replaceWith(replacement)
      finishSectionSettle(replacement)

      expect(document.activeElement).toBe(
        replacement.querySelector(`#btn-${kind}-c`),
      )
    },
  )

  it('falls back to the previous card and then to the section heading', () => {
    const previousSection = createReactionSection('likes', 'like', ['a', 'b'])
    document.body.appendChild(previousSection)
    const lastButton = previousSection.querySelector<HTMLButtonElement>('#btn-like-b')!
    startRemovalRequest(lastButton)
    dispatchRemoval('like', 'b')
    finishInteractionRequest(lastButton)

    const previousReplacement = createReactionSection('likes', 'like', ['a'])
    previousSection.replaceWith(previousReplacement)
    finishSectionSettle(previousReplacement)
    expect(document.activeElement).toBe(
      previousReplacement.querySelector('#btn-like-a'),
    )

    const onlyButton = previousReplacement.querySelector<HTMLButtonElement>('#btn-like-a')!
    startRemovalRequest(onlyButton)
    dispatchRemoval('like', 'a')
    finishInteractionRequest(onlyButton)
    const emptyReplacement = createReactionSection('likes', 'like', [])
    previousReplacement.replaceWith(emptyReplacement)
    finishSectionSettle(emptyReplacement)
    expect(document.activeElement).toBe(
      emptyReplacement.querySelector('#my-content-section-title'),
    )
  })

  it('does not arm a likes refresh focus for an unrelated active request', () => {
    const oldSection = createReactionSection('likes', 'like', ['a', 'b'])
    document.body.appendChild(oldSection)
    const button = oldSection.querySelector<HTMLButtonElement>('#btn-like-a')!
    button.value = 'active'
    startRemovalRequest(button)
    finishInteractionRequest(button)

    const replacement = createReactionSection('likes', 'like', ['b'])
    oldSection.replaceWith(replacement)
    finishSectionSettle(replacement)

    expect(document.activeElement).toBe(document.body)
  })

  it('clears pending focus when the removal request fails', () => {
    const oldSection = createReactionSection('likes', 'like', ['a', 'b'])
    document.body.appendChild(oldSection)
    const button = oldSection.querySelector<HTMLButtonElement>('#btn-like-a')!
    startRemovalRequest(button)
    dispatchFailure(button, 'htmx:responseError', {})
    finishInteractionRequest(button)

    const replacement = createReactionSection('likes', 'like', ['b'])
    oldSection.replaceWith(replacement)
    finishSectionSettle(replacement)

    expect(document.activeElement).toBe(document.body)
  })

  it('does not steal focus after the user moves outside the removing card', () => {
    const outsideButton = document.createElement('button')
    outsideButton.textContent = '其他操作'
    document.body.appendChild(outsideButton)
    const oldSection = createReactionSection('likes', 'like', ['a', 'b', 'c'])
    document.body.appendChild(oldSection)
    const button = oldSection.querySelector<HTMLButtonElement>('#btn-like-b')!
    startRemovalRequest(button)
    outsideButton.focus()
    dispatchRemoval('like', 'b')
    finishInteractionRequest(button)

    const replacement = createReactionSection('likes', 'like', ['a', 'c'])
    oldSection.replaceWith(replacement)
    finishSectionSettle(replacement)

    expect(document.activeElement).toBe(outsideButton)
  })

  it('keeps reversed concurrent responses tied to the latest focused removal', () => {
    const oldSection = createReactionSection('likes', 'like', ['a', 'b', 'c', 'd'])
    document.body.appendChild(oldSection)
    const buttonB = oldSection.querySelector<HTMLButtonElement>('#btn-like-b')!
    const buttonC = oldSection.querySelector<HTMLButtonElement>('#btn-like-c')!
    startRemovalRequest(buttonB)
    startRemovalRequest(buttonC)

    dispatchRemoval('like', 'c')
    dispatchRemoval('like', 'b')
    finishInteractionRequest(buttonC)
    finishInteractionRequest(buttonB)

    const replacement = createReactionSection('likes', 'like', ['a', 'd'])
    oldSection.replaceWith(replacement)
    finishSectionSettle(replacement)

    expect(document.activeElement).toBe(
      replacement.querySelector('#btn-like-d'),
    )
  })

  it('does not reuse another request plan when the latest removal fails', () => {
    const oldSection = createReactionSection('likes', 'like', ['a', 'b', 'c', 'd'])
    document.body.appendChild(oldSection)
    const buttonB = oldSection.querySelector<HTMLButtonElement>('#btn-like-b')!
    const buttonC = oldSection.querySelector<HTMLButtonElement>('#btn-like-c')!
    startRemovalRequest(buttonB)
    startRemovalRequest(buttonC)

    dispatchFailure(buttonC, 'htmx:responseError', {})
    finishInteractionRequest(buttonC)
    dispatchRemoval('like', 'b')
    finishInteractionRequest(buttonB)

    const replacement = createReactionSection('likes', 'like', ['a', 'c', 'd'])
    oldSection.replaceWith(replacement)
    finishSectionSettle(replacement)

    expect(document.activeElement).toBe(document.body)
  })

  it('keeps a pending refresh focus when another card request fails', () => {
    const oldSection = createReactionSection('likes', 'like', ['a', 'b', 'c'])
    document.body.appendChild(oldSection)
    const buttonB = oldSection.querySelector<HTMLButtonElement>('#btn-like-b')!
    const buttonC = oldSection.querySelector<HTMLButtonElement>('#btn-like-c')!
    startRemovalRequest(buttonB)
    dispatchRemoval('like', 'b')
    finishInteractionRequest(buttonB)

    dispatchFailure(buttonC, 'htmx:responseError', {})

    const notifications = document.querySelectorAll('[data-notification]')
    expect(notifications).toHaveLength(1)
    expect(notifications[0]?.textContent).toContain('点赞未完成，请稍后重试。')
    expect(notifications[0]?.textContent).not.toContain('列表刷新失败')

    const replacement = createReactionSection('likes', 'like', ['a', 'c'])
    oldSection.replaceWith(replacement)
    finishSectionSettle(replacement)

    expect(document.activeElement).toBe(
      replacement.querySelector('#btn-like-c'),
    )
  })

  it('reports a fixed refresh failure without leaking the response body', () => {
    const oldSection = createReactionSection('favorites', 'favorite', ['a', 'b'])
    document.body.appendChild(oldSection)
    const button = oldSection.querySelector<HTMLButtonElement>('#btn-favorite-a')!
    startRemovalRequest(button)
    dispatchRemoval('favorite', 'a')
    finishInteractionRequest(button)

    const xhr = { responseText: 'private upstream diagnostics' }
    dispatchSectionFailure(oldSection, 'htmx:responseError', xhr)

    const notification = document.querySelector('[data-notification]')
    expect(notification?.textContent).toContain(
      '互动状态已更新，但列表刷新失败，请刷新页面。',
    )
    expect(notification?.textContent).not.toContain('private upstream diagnostics')

    const replacement = createReactionSection('favorites', 'favorite', ['b'])
    oldSection.replaceWith(replacement)
    finishSectionSettle(replacement)
    expect(document.activeElement).toBe(document.body)
  })

  it('drops a focus plan when a successful response has no removal trigger', () => {
    const oldSection = createReactionSection('likes', 'like', ['a', 'b'])
    document.body.appendChild(oldSection)
    const button = oldSection.querySelector<HTMLButtonElement>('#btn-like-a')!
    startRemovalRequest(button)
    finishInteractionRequest(button)

    const replacement = createReactionSection('likes', 'like', ['b'])
    oldSection.replaceWith(replacement)
    finishSectionSettle(replacement)

    expect(document.activeElement).toBe(document.body)
  })

  it.each([
    ['htmx:responseError', true],
    ['htmx:sendError', false],
    ['htmx:timeout', false],
  ] as const)(
    'reports %s after another request detaches the interaction button',
    async (eventName, failureBeforeAfterRequest) => {
      const oldSection = createReactionSection('likes', 'like', ['a', 'b', 'c'])
      document.body.appendChild(oldSection)
      const buttonA = oldSection.querySelector<HTMLButtonElement>('#btn-like-a')!
      const buttonB = oldSection.querySelector<HTMLButtonElement>('#btn-like-b')!
      const xhrA = {}
      const xhrB = { responseText: 'private detached request diagnostics' }

      startRemovalRequest(buttonA, false, xhrA)
      startRemovalRequest(buttonB, true, xhrB)
      dispatchRemoval('like', 'a')
      finishInteractionRequest(buttonA, xhrA)

      const replacement = createReactionSection('likes', 'like', ['b', 'c'])
      oldSection.replaceWith(replacement)
      finishSectionSettle(replacement)
      expect(buttonB.isConnected).toBe(false)

      if (failureBeforeAfterRequest) {
        dispatchFailure(buttonB, eventName, xhrB)
        finishInteractionRequest(buttonB, xhrB)
      } else {
        finishInteractionRequest(buttonB, xhrB)
        dispatchFailure(buttonB, eventName, xhrB)
      }

      const notifications = document.querySelectorAll('[data-notification]')
      expect(notifications).toHaveLength(1)
      expect(notifications[0]?.textContent).toContain('点赞未完成，请稍后重试。')
      expect(notifications[0]?.textContent).not.toContain('private detached request diagnostics')

      await Promise.resolve()
      dispatchFailure(buttonB, eventName, {})
      expect(document.querySelectorAll('[data-notification]')).toHaveLength(1)

      dispatchRemoval('like', 'b')
      const finalReplacement = createReactionSection('likes', 'like', ['c'])
      replacement.replaceWith(finalReplacement)
      finishSectionSettle(finalReplacement)
      expect(document.activeElement).toBe(document.body)
    },
  )

  it('clears a detached request focus plan on afterRequest without a removal trigger', async () => {
    const oldSection = createReactionSection('favorites', 'favorite', ['a', 'b'])
    document.body.appendChild(oldSection)
    const button = oldSection.querySelector<HTMLButtonElement>('#btn-favorite-a')!
    const xhr = {}
    startRemovalRequest(button, true, xhr)

    const replacement = createReactionSection('favorites', 'favorite', ['b'])
    oldSection.replaceWith(replacement)
    expect(button.isConnected).toBe(false)
    finishInteractionRequest(button, xhr)
    await Promise.resolve()

    dispatchRemoval('favorite', 'a')
    const finalReplacement = createReactionSection('favorites', 'favorite', ['b'])
    replacement.replaceWith(finalReplacement)
    finishSectionSettle(finalReplacement)

    expect(document.activeElement).toBe(document.body)
    expect(document.querySelector('[data-notification]')).toBeNull()
  })
})
