// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { initializeModerationResolution } from './moderation-resolution'

function dispatchModalEvent(modal: HTMLElement, name: string, trigger?: HTMLElement): Event {
  const event = new Event(name, { bubbles: true, cancelable: true }) as Event & {
    relatedTarget?: EventTarget
  }
  if (trigger) {
    event.relatedTarget = trigger
  }
  modal.dispatchEvent(event)
  return event
}

describe('moderation resolution modal', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <button
        id="resolve"
        data-resolution-trigger
        data-resolution-action="/staff/reports/share-1/resolve/"
        data-resolution-title="认可举报"
        data-resolution-subject="分享：超长标题"
        data-resolution-context-label="当前限制："
        data-resolution-context="用户提供的举报内容"
        data-resolution-submit="确认认可"
        data-resolution-tone="danger"></button>
      <p id="report-reason-source">来自页面的&lt;img src=x onerror=alert(1)&gt;举报内容</p>
      <button
        id="dismiss"
        data-resolution-trigger
        data-resolution-action="/staff/reports/42/dismiss/"
        data-resolution-title="驳回举报"
        data-resolution-subject="举报人：long-user"
        data-resolution-submit="确认驳回"
        data-resolution-tone="secondary"></button>
      <div data-moderation-resolution-modal>
        <form data-resolution-form action="">
          <h2 data-resolution-modal-title></h2>
          <p data-resolution-modal-subject></p>
          <p data-resolution-modal-context>
            <strong data-resolution-modal-context-label></strong>
            <span data-resolution-modal-context-value></span>
          </p>
          <input type="hidden" name="version" data-resolution-version>
          <textarea name="reason"></textarea>
          <button type="submit" class="btn btn-secondary" data-resolution-modal-submit></button>
        </form>
      </div>
    `
  })

  it('configures one server form from each trigger without rendering user text as HTML', () => {
    const modal = document.querySelector<HTMLElement>('[data-moderation-resolution-modal]')!
    const form = modal.querySelector<HTMLFormElement>('form')!
    const reason = modal.querySelector<HTMLTextAreaElement>('textarea')!
    const resolve = document.querySelector<HTMLElement>('#resolve')!
    const dismiss = document.querySelector<HTMLElement>('#dismiss')!
    const submit = modal.querySelector<HTMLButtonElement>('[data-resolution-modal-submit]')!
    resolve.dataset.resolutionContext = '<img src=x onerror=alert(1)>'
    initializeModerationResolution()

    expect(submit.disabled).toBe(true)
    dispatchModalEvent(modal, 'show.bs.modal', resolve)

    expect(form.getAttribute('action')).toBe('/staff/reports/share-1/resolve/')
    expect(submit.disabled).toBe(false)
    expect(modal.querySelector('[data-resolution-modal-title]')?.textContent).toBe('认可举报')
    expect(modal.querySelector('[data-resolution-modal-context-value]')?.textContent)
      .toBe('<img src=x onerror=alert(1)>')
    expect(modal.querySelector('[data-resolution-modal-context-value] img')).toBeNull()
    expect(modal.querySelector('[data-resolution-modal-submit]')?.classList.contains('btn-danger'))
      .toBe(true)

    reason.value = '不会带到另一项操作'
    dispatchModalEvent(modal, 'show.bs.modal', dismiss)

    expect(form.getAttribute('action')).toBe('/staff/reports/42/dismiss/')
    expect(reason.value).toBe('')
    expect(modal.querySelector<HTMLElement>('[data-resolution-modal-context]')?.hidden).toBe(true)
    expect(modal.querySelector('[data-resolution-modal-submit]')?.classList.contains('btn-secondary'))
      .toBe(true)

    reason.value = '关闭时清空'
    dispatchModalEvent(modal, 'hidden.bs.modal')

    expect(form.getAttribute('action')).toBe('')
    expect(reason.value).toBe('')
    expect(submit.disabled).toBe(true)
  })

  it('rejects a cross-origin form action and focuses the reason after the modal is shown', () => {
    const modal = document.querySelector<HTMLElement>('[data-moderation-resolution-modal]')!
    const trigger = document.querySelector<HTMLElement>('#resolve')!
    const reason = modal.querySelector<HTMLTextAreaElement>('textarea')!
    const submit = modal.querySelector<HTMLButtonElement>('[data-resolution-modal-submit]')!
    trigger.dataset.resolutionAction = 'https://example.com/steal'
    initializeModerationResolution()

    const showEvent = dispatchModalEvent(modal, 'show.bs.modal', trigger)
    expect(showEvent.defaultPrevented).toBe(true)
    expect(submit.disabled).toBe(true)
    expect(modal.querySelector<HTMLFormElement>('form')?.getAttribute('action')).toBe('')

    const missingTriggerEvent = dispatchModalEvent(modal, 'show.bs.modal')
    expect(missingTriggerEvent.defaultPrevented).toBe(true)
    expect(submit.disabled).toBe(true)

    dispatchModalEvent(modal, 'shown.bs.modal')
    expect(document.activeElement).toBe(reason)
  })

  it('reads user context from a stable text source without duplicating or parsing HTML', () => {
    const modal = document.querySelector<HTMLElement>('[data-moderation-resolution-modal]')!
    const trigger = document.querySelector<HTMLElement>('#resolve')!
    trigger.dataset.resolutionContextSource = 'report-reason-source'
    trigger.dataset.resolutionContext = '不应优先使用的回退内容'
    initializeModerationResolution()

    dispatchModalEvent(modal, 'show.bs.modal', trigger)

    const value = modal.querySelector<HTMLElement>('[data-resolution-modal-context-value]')!
    expect(value.textContent).toBe('来自页面的<img src=x onerror=alert(1)>举报内容')
    expect(value.querySelector('img')).toBeNull()

    trigger.dataset.resolutionContextSource = 'missing-source'
    dispatchModalEvent(modal, 'show.bs.modal', trigger)
    expect(value.textContent).toBe('不应优先使用的回退内容')
  })

  it('sets a review concurrency version and success tone, then clears both on close', () => {
    const modal = document.querySelector<HTMLElement>('[data-moderation-resolution-modal]')!
    const trigger = document.querySelector<HTMLElement>('#resolve')!
    const version = modal.querySelector<HTMLInputElement>('[data-resolution-version]')!
    const submit = modal.querySelector<HTMLButtonElement>('[data-resolution-modal-submit]')!
    trigger.dataset.resolutionVersion = '2026-07-16T17:00:00+08:00'
    trigger.dataset.resolutionTone = 'success'
    initializeModerationResolution()

    dispatchModalEvent(modal, 'show.bs.modal', trigger)

    expect(version.value).toBe('2026-07-16T17:00:00+08:00')
    expect(submit.classList.contains('btn-success')).toBe(true)
    expect(submit.classList.contains('btn-danger')).toBe(false)
    expect(submit.classList.contains('btn-secondary')).toBe(false)

    dispatchModalEvent(modal, 'hidden.bs.modal')
    expect(version.value).toBe('')
    expect(submit.disabled).toBe(true)
  })

  it('closes the active modal before a boosted resolution request swaps the queue', () => {
    const modal = document.querySelector<HTMLElement>(
      '[data-moderation-resolution-modal]',
    )!
    const form = modal.querySelector<HTMLFormElement>('form')!
    const hide = vi.fn()
    window.bootstrap = {
      Modal: {
        getOrCreateInstance: vi.fn(() => ({ hide, show: vi.fn() })),
      },
    }
    initializeModerationResolution()

    document.dispatchEvent(new CustomEvent('htmx:beforeRequest', {
      detail: { elt: form },
    }))

    expect(hide).toHaveBeenCalledOnce()
  })

  it('reopens a server-invalid report action without clearing its reason and focuses errors', () => {
    const modal = document.querySelector<HTMLElement>('[data-moderation-resolution-modal]')!
    const form = modal.querySelector<HTMLFormElement>('form')!
    const reason = modal.querySelector<HTMLTextAreaElement>('textarea')!
    const submit = modal.querySelector<HTMLButtonElement>('[data-resolution-modal-submit]')!
    const errorSummary = document.createElement('div')
    errorSummary.id = 'report-resolution-errors'
    errorSummary.tabIndex = -1
    errorSummary.dataset.moderationErrorSummary = 'true'
    modal.querySelector('[data-resolution-modal-context]')?.after(errorSummary)
    const staleWarning = document.createElement('div')
    staleWarning.dataset.resolutionServerOnly = 'true'
    staleWarning.textContent = '目标状态已发生变化'
    errorSummary.after(staleWarning)
    modal.dataset.moderationInvalidModal = 'true'
    form.action = '/staff/reports/42/dismiss/'
    form.dataset.resolutionFallbackAction = '/staff/reports/'
    reason.value = '保留管理员已经填写的说明'
    reason.setAttribute('aria-invalid', 'true')
    reason.setAttribute(
      'aria-describedby',
      'report-resolution-help report-resolution-errors',
    )
    submit.disabled = false
    const show = vi.fn(() => {
      dispatchModalEvent(modal, 'show.bs.modal')
      dispatchModalEvent(modal, 'shown.bs.modal')
    })
    window.bootstrap = {
      Modal: { getOrCreateInstance: vi.fn(() => ({ hide: vi.fn(), show })) },
    }

    initializeModerationResolution()

    expect(show).toHaveBeenCalledOnce()
    expect(form.getAttribute('action')).toBe('/staff/reports/42/dismiss/')
    expect(reason.value).toBe('保留管理员已经填写的说明')
    expect(submit.disabled).toBe(false)
    expect(document.activeElement).toBe(errorSummary)

    dispatchModalEvent(modal, 'hidden.bs.modal')
    expect(reason.value).toBe('')
    expect(form.getAttribute('action')).toBe('/staff/reports/')
    expect(submit.disabled).toBe(true)
    expect(errorSummary.isConnected).toBe(false)
    expect(staleWarning.isConnected).toBe(false)
    expect(reason.hasAttribute('aria-invalid')).toBe(false)
    expect(reason.getAttribute('aria-describedby')).toBe('report-resolution-help')
    expect(document.activeElement).toBe(document.querySelector('#dismiss'))

    const resolve = document.querySelector<HTMLElement>('#resolve')!
    dispatchModalEvent(modal, 'show.bs.modal', resolve)
    dispatchModalEvent(modal, 'shown.bs.modal')
    expect(form.getAttribute('action')).toBe('/staff/reports/share-1/resolve/')
    expect(modal.querySelector('[data-resolution-modal-title]')?.textContent).toBe('认可举报')
    expect(modal.querySelector('[data-resolution-modal-subject]')?.textContent)
      .toBe('分享：超长标题')
    expect(modal.querySelector('[data-moderation-error-summary]')).toBeNull()
    expect(modal.querySelector('[data-resolution-server-only]')).toBeNull()
    expect(document.activeElement).toBe(reason)
  })

  it('reopens a server-invalid shared review action with its version and exact focus target', () => {
    const modal = document.querySelector<HTMLElement>('[data-moderation-resolution-modal]')!
    const form = modal.querySelector<HTMLFormElement>('form')!
    const reason = modal.querySelector<HTMLTextAreaElement>('textarea')!
    const version = modal.querySelector<HTMLInputElement>('[data-resolution-version]')!
    const trigger = document.querySelector<HTMLElement>('#resolve')!
    const errorSummary = document.createElement('div')
    errorSummary.tabIndex = -1
    errorSummary.dataset.moderationErrorSummary = 'true'
    modal.querySelector('[data-resolution-modal-context]')?.after(errorSummary)
    trigger.dataset.resolutionAction = '/staff/restrictions/share-1/confirm/'
    modal.dataset.moderationInvalidModal = 'true'
    form.action = '/staff/restrictions/share-1/confirm/'
    form.dataset.resolutionFallbackAction = '/staff/reviews/'
    reason.value = '保留人工确认说明'
    version.value = '2026-07-16T17:00:00+08:00'
    const show = vi.fn(() => {
      dispatchModalEvent(modal, 'show.bs.modal')
      dispatchModalEvent(modal, 'shown.bs.modal')
    })
    window.bootstrap = {
      Modal: { getOrCreateInstance: vi.fn(() => ({ hide: vi.fn(), show })) },
    }

    initializeModerationResolution()

    expect(show).toHaveBeenCalledOnce()
    expect(reason.value).toBe('保留人工确认说明')
    expect(version.value).toBe('2026-07-16T17:00:00+08:00')
    expect(document.activeElement).toBe(errorSummary)

    dispatchModalEvent(modal, 'hidden.bs.modal')
    expect(reason.value).toBe('')
    expect(version.value).toBe('')
    expect(form.getAttribute('action')).toBe('/staff/reviews/')
    expect(document.activeElement).toBe(trigger)
  })

  it('returns an unmatched server-invalid shared action to main content', () => {
    const main = document.createElement('main')
    main.id = 'main-content'
    main.tabIndex = -1
    document.body.prepend(main)
    const modal = document.querySelector<HTMLElement>('[data-moderation-resolution-modal]')!
    const form = modal.querySelector<HTMLFormElement>('form')!
    modal.dataset.moderationInvalidModal = 'true'
    form.action = '/staff/reviews/missing-action/'
    const show = vi.fn(() => {
      dispatchModalEvent(modal, 'show.bs.modal')
      dispatchModalEvent(modal, 'shown.bs.modal')
    })
    window.bootstrap = {
      Modal: { getOrCreateInstance: vi.fn(() => ({ hide: vi.fn(), show })) },
    }

    initializeModerationResolution()
    dispatchModalEvent(modal, 'hidden.bs.modal')

    expect(show).toHaveBeenCalledOnce()
    expect(document.activeElement).toBe(main)
  })
})
