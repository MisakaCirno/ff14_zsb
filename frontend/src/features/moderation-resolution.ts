import { getBootstrapModal } from '../core/bootstrap'

type ResolutionTone = 'danger' | 'secondary' | 'success'

interface BootstrapModalEvent extends Event {
  relatedTarget?: EventTarget | null
}

interface HtmxBeforeRequestDetail {
  elt?: Element
}

let resolutionLifecycleInitialized = false

function resolutionTriggerFrom(event: Event): HTMLElement | null {
  const relatedTarget = (event as BootstrapModalEvent).relatedTarget
  return relatedTarget instanceof HTMLElement
    && relatedTarget.matches('[data-resolution-trigger]')
    ? relatedTarget
    : null
}

function localActionPath(value: string): string | null {
  try {
    const url = new URL(value, window.location.href)
    if (url.origin !== window.location.origin) {
      return null
    }
    return `${url.pathname}${url.search}${url.hash}`
  } catch {
    return null
  }
}

function updateSubmitTone(button: HTMLButtonElement, tone: string | undefined): void {
  const resolvedTone: ResolutionTone = tone === 'danger' || tone === 'success'
    ? tone
    : 'secondary'
  button.classList.toggle('btn-danger', resolvedTone === 'danger')
  button.classList.toggle('btn-secondary', resolvedTone === 'secondary')
  button.classList.toggle('btn-success', resolvedTone === 'success')
}

function resolutionContext(trigger: HTMLElement): string {
  const sourceId = trigger.dataset.resolutionContextSource
  if (sourceId) {
    const source = document.getElementById(sourceId)
    if (source) {
      return source.textContent?.trim() ?? ''
    }
  }
  return trigger.dataset.resolutionContext ?? ''
}

function focusModalError(modal: HTMLElement, fallback?: HTMLElement): void {
  const errorSummary = modal.querySelector<HTMLElement>('[data-moderation-error-summary]')
  if (errorSummary) {
    errorSummary.focus({ preventScroll: true })
    return
  }
  fallback?.focus({ preventScroll: true })
}

function matchingResolutionTrigger(action: string): HTMLElement | null {
  return Array.from(
    document.querySelectorAll<HTMLElement>(
      '[data-resolution-trigger][data-resolution-action]',
    ),
  ).find((trigger) => (
    localActionPath(trigger.dataset.resolutionAction ?? '') === action
  )) ?? null
}

function restoreRecoveryFocus(preferred: HTMLElement | null): void {
  const target = preferred?.isConnected
    ? preferred
    : document.querySelector<HTMLElement>('#main-content')
  target?.focus({ preventScroll: true })
}

function initializeResolutionModal(modal: HTMLElement): void {
  if (modal.dataset.resolutionInitialized === 'true') {
    return
  }

  const form = modal.querySelector<HTMLFormElement>('[data-resolution-form]')
  const title = modal.querySelector<HTMLElement>('[data-resolution-modal-title]')
  const subject = modal.querySelector<HTMLElement>('[data-resolution-modal-subject]')
  const context = modal.querySelector<HTMLElement>('[data-resolution-modal-context]')
  const contextLabel = modal.querySelector<HTMLElement>('[data-resolution-modal-context-label]')
  const contextValue = modal.querySelector<HTMLElement>('[data-resolution-modal-context-value]')
  const reason = modal.querySelector<HTMLTextAreaElement>('textarea[name="reason"]')
  const version = modal.querySelector<HTMLInputElement>('[data-resolution-version]')
  const submit = modal.querySelector<HTMLButtonElement>('[data-resolution-modal-submit]')
  if (
    !form
    || !title
    || !subject
    || !context
    || !contextLabel
    || !contextValue
    || !reason
    || !submit
  ) {
    return
  }

  const fallbackAction = form.dataset.resolutionFallbackAction
    ?? form.getAttribute('action')
    ?? ''
  const serverErrorAction = modal.hasAttribute('data-moderation-invalid-modal')
    ? localActionPath(form.getAttribute('action') ?? '')
    : null
  const serverReturnFocus = serverErrorAction
    ? matchingResolutionTrigger(serverErrorAction)
    : null
  let serverErrorPending = serverErrorAction !== null
  let restoreServerFocusOnHide = false
  const clearServerRecoveryState = (): void => {
    const errorSummary = modal.querySelector<HTMLElement>('[data-moderation-error-summary]')
    const errorId = errorSummary?.id
    errorSummary?.remove()
    modal.querySelectorAll('[data-resolution-server-only]').forEach((element) => {
      element.remove()
    })
    reason.removeAttribute('aria-invalid')
    if (errorId) {
      const describedBy = (reason.getAttribute('aria-describedby') ?? '')
        .split(/\s+/)
        .filter((id) => id && id !== errorId)
      if (describedBy.length > 0) {
        reason.setAttribute('aria-describedby', describedBy.join(' '))
      } else {
        reason.removeAttribute('aria-describedby')
      }
    }
  }
  const resetResolutionForm = (): void => {
    form.reset()
    reason.value = ''
    if (version) {
      version.value = ''
    }
    form.setAttribute('action', fallbackAction)
    submit.disabled = true
    clearServerRecoveryState()
  }

  modal.dataset.resolutionInitialized = 'true'
  if (!serverErrorPending) {
    resetResolutionForm()
  }
  modal.addEventListener('show.bs.modal', (event) => {
    const trigger = resolutionTriggerFrom(event)
    if (!trigger && serverErrorPending && serverErrorAction) {
      form.setAttribute('action', serverErrorAction)
      submit.disabled = false
      serverErrorPending = false
      restoreServerFocusOnHide = true
      return
    }

    restoreServerFocusOnHide = false
    resetResolutionForm()
    const action = trigger?.dataset.resolutionAction
      ? localActionPath(trigger.dataset.resolutionAction)
      : null
    if (!trigger || !action) {
      event.preventDefault()
      return
    }

    form.setAttribute('action', action)
    title.textContent = trigger.dataset.resolutionTitle ?? '处理举报'
    subject.textContent = trigger.dataset.resolutionSubject ?? ''
    submit.textContent = trigger.dataset.resolutionSubmit ?? '确认处理'
    updateSubmitTone(submit, trigger.dataset.resolutionTone)
    if (version) {
      version.value = trigger.dataset.resolutionVersion ?? ''
    }

    const detail = resolutionContext(trigger)
    context.hidden = detail.length === 0
    contextLabel.textContent = trigger.dataset.resolutionContextLabel ?? ''
    contextValue.textContent = detail
    submit.disabled = false
  })
  modal.addEventListener('shown.bs.modal', () => {
    focusModalError(modal, reason)
  })
  modal.addEventListener('hidden.bs.modal', () => {
    resetResolutionForm()
    if (restoreServerFocusOnHide) {
      restoreServerFocusOnHide = false
      restoreRecoveryFocus(serverReturnFocus)
    }
  })

  if (serverErrorPending) {
    getBootstrapModal(modal)?.show()
  }
}

export function initializeModerationResolution(): void {
  if (!resolutionLifecycleInitialized) {
    resolutionLifecycleInitialized = true
    document.addEventListener('htmx:beforeRequest', (event) => {
      const detail = (event as CustomEvent<HtmxBeforeRequestDetail>).detail
      const form = detail?.elt instanceof HTMLFormElement
        ? detail.elt
        : detail?.elt?.closest<HTMLFormElement>('[data-resolution-form]')
      if (!form?.matches('[data-resolution-form]')) {
        return
      }
      const modal = form.closest<HTMLElement>(
        '[data-moderation-resolution-modal]',
      )
      if (modal) {
        getBootstrapModal(modal)?.hide()
      }
    })
  }
  document.querySelectorAll<HTMLElement>('[data-moderation-resolution-modal]')
    .forEach(initializeResolutionModal)
}
