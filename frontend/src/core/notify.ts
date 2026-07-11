const allowedTypes = new Set(['danger', 'info', 'primary', 'success', 'warning'])

export function showMessage(message: unknown, type = 'success'): void {
  const container = document.getElementById('message-container')
  if (!container) {
    return
  }

  const safeType = allowedTypes.has(type) ? type : 'info'
  const alert = document.createElement('div')
  const messageText = document.createElement('span')
  const closeButton = document.createElement('button')

  alert.className = `alert alert-${safeType} alert-dismissible fade show shadow`
  alert.role = 'alert'
  messageText.style.whiteSpace = 'pre-line'
  messageText.textContent = String(message ?? '')
  closeButton.type = 'button'
  closeButton.className = 'btn-close'
  closeButton.setAttribute('aria-label', '关闭')

  const dismiss = (): void => {
    alert.classList.remove('show')
    window.setTimeout(() => alert.remove(), 150)
  }

  closeButton.addEventListener('click', dismiss)
  alert.append(messageText, closeButton)
  container.appendChild(alert)
  window.setTimeout(dismiss, 3000)
}
