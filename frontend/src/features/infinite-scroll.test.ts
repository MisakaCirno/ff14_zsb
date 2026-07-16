// @vitest-environment jsdom

import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { initializeInfiniteScrollStatus } from './infinite-scroll'

interface ScrollFixture {
  region: HTMLElement
  sentinel: HTMLButtonElement
}

type XhrHandler = ((event: Event) => void) | null

class ControlledXMLHttpRequest extends EventTarget {
  static instances: ControlledXMLHttpRequest[] = []

  onabort: XhrHandler = null
  onerror: XhrHandler = null
  onload: XhrHandler = null
  ontimeout: XhrHandler = null
  response = ''
  responseText = ''
  responseURL = ''
  status = 0
  timeout = 0
  upload = new EventTarget()
  withCredentials = false

  constructor() {
    super()
    ControlledXMLHttpRequest.instances.push(this)
  }

  abort(): void {
    this.onabort?.(new Event('abort'))
  }

  fail(): void {
    this.onerror?.(new Event('error'))
  }

  getAllResponseHeaders(): string {
    return ''
  }

  getResponseHeader(): string | null {
    return null
  }

  open(): void {}

  overrideMimeType(): void {}

  send(): void {}

  setRequestHeader(): void {}
}

class ControlledIntersectionObserver implements IntersectionObserver {
  static instances: ControlledIntersectionObserver[] = []

  readonly root = null
  readonly rootMargin = '0px'
  readonly scrollMargin = '0px'
  readonly thresholds = [0]

  constructor(private readonly callback: IntersectionObserverCallback) {
    ControlledIntersectionObserver.instances.push(this)
  }

  disconnect(): void {}

  observe(): void {}

  takeRecords(): IntersectionObserverEntry[] {
    return []
  }

  trigger(target: Element): void {
    this.callback([{
      boundingClientRect: target.getBoundingClientRect(),
      intersectionRatio: 1,
      intersectionRect: target.getBoundingClientRect(),
      isIntersecting: true,
      rootBounds: null,
      target,
      time: 0,
    }], this)
  }

  unobserve(): void {}
}

function renderScrollFixture(): ScrollFixture {
  document.body.innerHTML = `
    <main data-infinite-scroll-region>
      <article data-share-card>
        <a href="/share/existing">Existing share</a>
      </article>
      <button type="button" data-infinite-scroll-sentinel>
        <span data-infinite-scroll-message>Load more</span>
      </button>
    </main>
  `
  return {
    region: document.querySelector<HTMLElement>('[data-infinite-scroll-region]')!,
    sentinel: document.querySelector<HTMLButtonElement>(
      '[data-infinite-scroll-sentinel]',
    )!,
  }
}

function startRequest(
  sentinel: HTMLButtonElement,
  xhr: object,
  key: ' ' | 'Enter' | null = 'Enter',
): void {
  sentinel.focus()
  if (key) {
    sentinel.dispatchEvent(new KeyboardEvent('keydown', {
      bubbles: true,
      key,
    }))
  }
  dispatchBeforeRequest(sentinel, xhr)
}

function dispatchBeforeRequest(sentinel: HTMLButtonElement, xhr: object): void {
  sentinel.dispatchEvent(new CustomEvent('htmx:beforeRequest', {
    bubbles: true,
    detail: {
      elt: sentinel,
      requestConfig: { elt: sentinel },
      xhr,
    },
  }))
}

function settleRequest(
  region: HTMLElement,
  sentinel: HTMLButtonElement,
  xhr: object,
): void {
  region.dispatchEvent(new CustomEvent('htmx:afterSettle', {
    bubbles: true,
    detail: {
      elt: region,
      requestConfig: { elt: sentinel },
      xhr,
    },
  }))
}

function newShareCard(path: string): HTMLElement {
  const card = document.createElement('article')
  card.dataset.shareCard = ''
  const link = document.createElement('a')
  link.href = path
  link.textContent = 'New share'
  card.appendChild(link)
  return card
}

describe('infinite scroll keyboard focus', () => {
  beforeAll(() => {
    initializeInfiniteScrollStatus()
  })

  beforeEach(() => {
    document.body.innerHTML = ''
  })

  it('moves focus to the first newly loaded card after keyboard activation', () => {
    const { region, sentinel } = renderScrollFixture()
    const xhr = {}
    startRequest(sentinel, xhr)

    const card = newShareCard('/share/new')
    const nextSentinel = document.createElement('button')
    nextSentinel.dataset.infiniteScrollSentinel = ''
    sentinel.replaceWith(card, nextSentinel)
    settleRequest(region, sentinel, xhr)

    expect(document.activeElement).toBe(card.querySelector('a'))
  })

  it('focuses the replacement sentinel when a response has no new card', () => {
    const { region, sentinel } = renderScrollFixture()
    const xhr = {}
    startRequest(sentinel, xhr, ' ')

    const nextSentinel = document.createElement('button')
    nextSentinel.dataset.infiniteScrollSentinel = ''
    sentinel.replaceWith(nextSentinel)
    settleRequest(region, sentinel, xhr)

    expect(document.activeElement).toBe(nextSentinel)
  })

  it('keeps Space intent through the keyup default-action window', () => {
    const { region, sentinel } = renderScrollFixture()
    const xhr = {}
    sentinel.focus()
    sentinel.dispatchEvent(new KeyboardEvent('keydown', {
      bubbles: true,
      key: ' ',
    }))
    sentinel.dispatchEvent(new KeyboardEvent('keyup', {
      bubbles: true,
      key: ' ',
    }))

    // A browser synthesizes the activation click before this keyup task reaches
    // its microtask checkpoint. The resulting request must still see the intent.
    dispatchBeforeRequest(sentinel, xhr)
    const card = newShareCard('/share/space')
    sentinel.replaceWith(card)
    settleRequest(region, sentinel, xhr)

    expect(document.activeElement).toBe(card.querySelector('a'))
  })

  it('clears an abandoned keyboard intent before a later automatic load', async () => {
    const { region, sentinel } = renderScrollFixture()
    sentinel.focus()
    sentinel.dispatchEvent(new KeyboardEvent('keydown', {
      bubbles: true,
      key: ' ',
    }))
    sentinel.dispatchEvent(new KeyboardEvent('keyup', {
      bubbles: true,
      key: ' ',
    }))
    await Promise.resolve()

    const outside = document.createElement('button')
    document.body.prepend(outside)
    outside.focus()
    const xhr = {}
    dispatchBeforeRequest(sentinel, xhr)
    const card = newShareCard('/share/automatic-after-keyup')
    sentinel.replaceWith(card)
    settleRequest(region, sentinel, xhr)

    expect(document.activeElement).toBe(outside)
  })

  it('focuses the terminal status when the last page has no card', () => {
    const { region, sentinel } = renderScrollFixture()
    const xhr = {}
    startRequest(sentinel, xhr)

    const end = document.createElement('div')
    end.dataset.infiniteScrollEnd = ''
    end.tabIndex = -1
    end.setAttribute('role', 'status')
    sentinel.replaceWith(end)
    settleRequest(region, sentinel, xhr)

    expect(document.activeElement).toBe(end)
  })

  it.each(['htmx:responseError', 'htmx:sendError', 'htmx:timeout'])(
    'restores the retry button after a keyboard %s',
    (eventName) => {
      const { sentinel } = renderScrollFixture()
      const xhr = {}
      startRequest(sentinel, xhr)
      sentinel.blur()

      sentinel.dispatchEvent(new CustomEvent(eventName, {
        bubbles: true,
        detail: {
          elt: sentinel,
          requestConfig: { elt: sentinel },
          xhr,
        },
      }))

      expect(sentinel.disabled).toBe(false)
      expect(sentinel.hasAttribute('aria-busy')).toBe(false)
      expect(sentinel.querySelector('[data-infinite-scroll-message]')?.textContent)
        .toBe('加载失败，点击重试')
      expect(document.activeElement).toBe(sentinel)
    },
  )

  it('does not move focus for an automatic intersect load', () => {
    const { region, sentinel } = renderScrollFixture()
    const outside = document.createElement('button')
    document.body.prepend(outside)
    outside.focus()
    const xhr = {}
    dispatchBeforeRequest(sentinel, xhr)

    const card = newShareCard('/share/automatic')
    sentinel.replaceWith(card)
    settleRequest(region, sentinel, xhr)

    expect(document.activeElement).toBe(outside)
  })

  it('does not treat a pointer request as the preceding keyboard intent', () => {
    const { region, sentinel } = renderScrollFixture()
    const xhr = {}
    sentinel.focus()
    sentinel.dispatchEvent(new KeyboardEvent('keydown', {
      bubbles: true,
      key: 'Enter',
    }))
    sentinel.dispatchEvent(new Event('pointerdown', { bubbles: true }))
    dispatchBeforeRequest(sentinel, xhr)

    const card = newShareCard('/share/pointer')
    sentinel.replaceWith(card)
    settleRequest(region, sentinel, xhr)

    expect(document.activeElement).not.toBe(card.querySelector('a'))
  })

  it('does not steal focus when the user moves elsewhere during loading', () => {
    const { region, sentinel } = renderScrollFixture()
    const outside = document.createElement('button')
    document.body.prepend(outside)
    const xhr = {}
    startRequest(sentinel, xhr)
    outside.focus()

    const card = newShareCard('/share/new')
    sentinel.replaceWith(card)
    settleRequest(region, sentinel, xhr)

    expect(document.activeElement).toBe(outside)
  })
})

describe('infinite scroll HTMX request synchronization', () => {
  beforeEach(() => {
    ControlledXMLHttpRequest.instances = []
    ControlledIntersectionObserver.instances = []
    vi.stubGlobal('XMLHttpRequest', ControlledXMLHttpRequest)
    vi.stubGlobal('IntersectionObserver', ControlledIntersectionObserver)
    const evaluate = window.XPathExpression.prototype.evaluate
    vi.spyOn(window.XPathExpression.prototype, 'evaluate').mockImplementation(
      function evaluateWithBrowserDefault(
        this: XPathExpression,
        contextNode,
        type = XPathResult.ANY_TYPE,
        result = null,
      ) {
        return evaluate.call(this, contextNode, type, result)
      },
    )
  })

  it('drops a repeated intersect while a keyboard request is in flight', async () => {
    document.body.innerHTML = `
      <main data-infinite-scroll-region>
        <article data-share-card>
          <a href="/share/existing">Existing share</a>
        </article>
        <button
          type="button"
          data-infinite-scroll-sentinel
          hx-get="/more"
          hx-trigger="intersect, click"
          hx-swap="outerHTML"
          hx-sync="this:drop">
          <span data-infinite-scroll-message>Load more</span>
        </button>
      </main>
    `
    const sentinel = document.querySelector<HTMLButtonElement>(
      '[data-infinite-scroll-sentinel]',
    )!
    const { default: htmx } = await import('htmx.org')
    expect(htmx.version).toBe('2.0.10')
    htmx.process(document.body)

    sentinel.focus()
    sentinel.dispatchEvent(new KeyboardEvent('keydown', {
      bubbles: true,
      key: 'Enter',
    }))
    sentinel.click()
    expect(ControlledXMLHttpRequest.instances).toHaveLength(1)
    expect(sentinel.disabled).toBe(true)

    const observer = ControlledIntersectionObserver.instances.at(-1)
    expect(observer).toBeDefined()
    observer!.trigger(sentinel)
    expect(ControlledXMLHttpRequest.instances).toHaveLength(1)

    ControlledXMLHttpRequest.instances[0]!.fail()

    expect(ControlledXMLHttpRequest.instances).toHaveLength(1)
    expect(sentinel.disabled).toBe(false)
    expect(sentinel.hasAttribute('aria-busy')).toBe(false)
    expect(sentinel.querySelector('[data-infinite-scroll-message]')?.textContent)
      .toBe('加载失败，点击重试')
    expect(document.activeElement).toBe(sentinel)
  })
})
