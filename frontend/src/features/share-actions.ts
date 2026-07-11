import { copyText } from '../core/clipboard'
import { getCsrfToken } from '../core/csrf'
import { showMessage } from '../core/notify'

function recordCopy(shareId: string): void {
  const csrfToken = getCsrfToken()
  if (!csrfToken) {
    return
  }

  void fetch(`/share/${encodeURIComponent(shareId)}/copy/`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': csrfToken,
    },
  }).catch(() => undefined)
}

function showCopiedButton(button: HTMLButtonElement): void {
  const originalChildren = Array.from(button.childNodes, (node) => node.cloneNode(true))
  const icon = document.createElement('i')
  icon.className = 'bi bi-check'
  button.replaceChildren(icon)
  button.classList.remove('btn-outline-primary')
  button.classList.add('btn-success')

  window.setTimeout(() => {
    button.replaceChildren(...originalChildren)
    button.classList.remove('btn-success')
    button.classList.add('btn-outline-primary')
  }, 2000)
}

function copyStrategyCode(button: HTMLButtonElement): void {
  const code = button.dataset.copyCode
  if (code === undefined) {
    return
  }

  void copyText(code, () => {
    showCopiedButton(button)
    showMessage('战术板代码已复制', 'success')
    if (button.dataset.shareId) {
      recordCopy(button.dataset.shareId)
    }
  })
}

function copyQqGroup(button: HTMLButtonElement): void {
  const qqGroup = button.dataset.copyQq
  if (!qqGroup) {
    return
  }
  const originalText = button.textContent ?? ''

  void copyText(qqGroup, () => {
    showMessage(`QQ群号 ${qqGroup} 已复制到剪贴板！`, 'success')
    button.textContent = '已复制！'
    window.setTimeout(() => {
      button.textContent = originalText
    }, 2000)
  })
}

export function initializeShareActions(): void {
  document.addEventListener('click', (event) => {
    if (!(event.target instanceof Element)) {
      return
    }

    const strategyButton = event.target.closest<HTMLButtonElement>('[data-copy-strategy]')
    if (strategyButton) {
      event.preventDefault()
      event.stopPropagation()
      copyStrategyCode(strategyButton)
      return
    }

    const qqButton = event.target.closest<HTMLButtonElement>('[data-copy-qq]')
    if (qqButton) {
      event.preventDefault()
      copyQqGroup(qqButton)
    }
  })
}
