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

  const fallbackAction = form.getAttribute('action') ?? ''
  const resetResolutionForm = (): void => {
    form.reset()
    form.setAttribute('action', fallbackAction)
    submit.disabled = true
  }

  modal.dataset.resolutionInitialized = 'true'
  resetResolutionForm()
  modal.addEventListener('show.bs.modal', (event) => {
    resetResolutionForm()
    const trigger = resolutionTriggerFrom(event)
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
    reason.focus({ preventScroll: true })
  })
  modal.addEventListener('hidden.bs.modal', resetResolutionForm)
}

export function initializeModerationResolution(): void {
  document.querySelectorAll<HTMLElement>('[data-moderation-resolution-modal]')
    .forEach(initializeResolutionModal)
}
