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

  it('opens only the server-marked review modal and focuses its error summary', () => {
    document.body.innerHTML = `
      <div id="unrelated" class="modal"><div tabindex="-1"></div></div>
      <button id="review-return" data-bs-target="#invalid-review">检查拒绝操作</button>
      <div id="invalid-review" class="modal" data-moderation-invalid-modal>
        <div data-moderation-error-summary tabindex="-1">请检查拒绝原因</div>
      </div>
    `
    const invalidModal = document.querySelector<HTMLElement>('#invalid-review')!
    const summary = invalidModal.querySelector<HTMLElement>(
      '[data-moderation-error-summary]',
    )!
    const show = vi.fn(() => {
      dispatchModalEvent(invalidModal, 'shown.bs.modal')
    })
    const getOrCreateInstance = vi.fn(() => ({ hide: vi.fn(), show }))
    window.bootstrap = { Modal: { getOrCreateInstance } }

    initializeModerationResolution()

    expect(getOrCreateInstance).toHaveBeenCalledOnce()
    expect(getOrCreateInstance).toHaveBeenCalledWith(invalidModal)
    expect(show).toHaveBeenCalledOnce()
    expect(document.activeElement).toBe(summary)

    dispatchModalEvent(invalidModal, 'hidden.bs.modal')
    expect(document.activeElement).toBe(document.querySelector('#review-return'))
  })

  it('returns a stale recovery modal without a matching trigger to main content once', () => {
    document.body.innerHTML = `
      <main id="main-content" tabindex="-1"></main>
      <div id="stale-review" class="modal" data-moderation-invalid-modal>
        <div data-moderation-error-summary tabindex="-1">目标状态已经变化</div>
      </div>
    `
    const modal = document.querySelector<HTMLElement>('#stale-review')!
    const main = document.querySelector<HTMLElement>('#main-content')!
    const show = vi.fn(() => {
      dispatchModalEvent(modal, 'shown.bs.modal')
    })
    window.bootstrap = {
      Modal: { getOrCreateInstance: vi.fn(() => ({ hide: vi.fn(), show })) },
    }

    initializeModerationResolution()
    dispatchModalEvent(modal, 'hidden.bs.modal')

    expect(document.activeElement).toBe(main)

    const userTrigger = document.createElement('button')
    document.body.append(userTrigger)
    userTrigger.focus()
    dispatchModalEvent(modal, 'show.bs.modal', userTrigger)
    dispatchModalEvent(modal, 'hidden.bs.modal')
    expect(document.activeElement).toBe(userTrigger)
  })
})
