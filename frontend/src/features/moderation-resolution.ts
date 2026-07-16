import { getBootstrapModal } from '../core/bootstrap'

type ResolutionTone = 'danger' | 'secondary'

interface BootstrapModalEvent extends Event {
  relatedTarget?: EventTarget | null
}

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
  const resolvedTone: ResolutionTone = tone === 'danger' ? 'danger' : 'secondary'
  button.classList.toggle('btn-danger', resolvedTone === 'danger')
  button.classList.toggle('btn-secondary', resolvedTone === 'secondary')
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

function matchingModalTrigger(modal: HTMLElement): HTMLElement | null {
  if (!modal.id) {
    return null
  }
  const target = `#${modal.id}`
  return Array.from(
    document.querySelectorAll<HTMLElement>('[data-bs-target]'),
  ).find((trigger) => trigger.dataset.bsTarget === target) ?? null
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

    const detail = trigger.dataset.resolutionContext ?? ''
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

function initializeInvalidReviewModal(modal: HTMLElement): void {
  if (modal.dataset.moderationInvalidInitialized === 'true') {
    return
  }
  modal.dataset.moderationInvalidInitialized = 'true'
  const returnFocus = matchingModalTrigger(modal)
  let restoreServerFocusOnHide = true
  modal.addEventListener('show.bs.modal', (event) => {
    if (resolutionTriggerFrom(event) || (event as BootstrapModalEvent).relatedTarget) {
      restoreServerFocusOnHide = false
    }
  })
  modal.addEventListener('shown.bs.modal', () => focusModalError(modal))
  modal.addEventListener('hidden.bs.modal', () => {
    if (restoreServerFocusOnHide) {
      restoreServerFocusOnHide = false
      restoreRecoveryFocus(returnFocus)
    }
  })
  getBootstrapModal(modal)?.show()
}

export function initializeModerationResolution(): void {
  document.querySelectorAll<HTMLElement>('[data-moderation-resolution-modal]')
    .forEach(initializeResolutionModal)
  document.querySelectorAll<HTMLElement>(
    '[data-moderation-invalid-modal]:not([data-moderation-resolution-modal])',
  ).forEach(initializeInvalidReviewModal)
}
