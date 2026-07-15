export function initializeAccountForms(): void {
  const errorSummary = document.querySelector<HTMLElement>('[data-account-error-summary]')
  errorSummary?.focus()
}
