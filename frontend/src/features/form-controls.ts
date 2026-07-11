function submitChangedForm(control: HTMLElement): void {
  const form = control instanceof HTMLInputElement || control instanceof HTMLSelectElement
    ? control.form
    : control.closest('form')
  form?.requestSubmit()
}

export function initializeFormControls(): void {
  document.addEventListener('change', (event) => {
    const control = event.target instanceof Element
      ? event.target.closest<HTMLElement>('[data-submit-on-change]')
      : null
    if (control) {
      submitChangedForm(control)
    }
  })

  document.addEventListener('submit', (event) => {
    if (!(event.target instanceof HTMLFormElement)) {
      return
    }
    const message = event.target.dataset.confirmMessage
    if (message && !window.confirm(message)) {
      event.preventDefault()
    }
  })
}
