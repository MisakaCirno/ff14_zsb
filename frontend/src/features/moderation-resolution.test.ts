// @vitest-environment jsdom

import { beforeEach, describe, expect, it } from 'vitest'

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
})
