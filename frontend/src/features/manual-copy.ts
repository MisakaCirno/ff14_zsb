import { getBootstrapModal } from '../core/bootstrap'
import { showMessage } from '../core/notify'

interface ManualCopyElements {
  label: HTMLElement
  modal: HTMLElement
  selectButton: HTMLButtonElement
  textArea: HTMLTextAreaElement
}

let elements: ManualCopyElements | null = null
let initialized = false
let returnFocusTarget: HTMLElement | null = null

function findElements(): ManualCopyElements | null {
  const modal = document.querySelector<HTMLElement>('[data-manual-copy-modal]')
  const label = modal?.querySelector<HTMLElement>('[data-manual-copy-label]')
  const textArea = modal?.querySelector<HTMLTextAreaElement>('[data-manual-copy-text]')
  const selectButton = modal?.querySelector<HTMLButtonElement>('[data-manual-copy-select]')
  if (!modal || !label || !textArea || !selectButton) {
    return null
  }
  return { label, modal, selectButton, textArea }
}

function selectManualCopyText(textArea: HTMLTextAreaElement): void {
  textArea.focus({ preventScroll: true })
  textArea.select()
  textArea.setSelectionRange(0, textArea.value.length)
}

export function initializeManualCopy(): void {
  if (initialized) {
    return
  }

  elements = findElements()
  if (!elements) {
    return
  }
  initialized = true

  elements.selectButton.addEventListener('click', () => {
    if (elements) {
      selectManualCopyText(elements.textArea)
    }
  })
  elements.modal.addEventListener('shown.bs.modal', () => {
    if (elements) {
      selectManualCopyText(elements.textArea)
    }
  })
  elements.modal.addEventListener('hidden.bs.modal', () => {
    if (elements) {
      elements.textArea.value = ''
      elements.label.textContent = '待复制内容'
    }
    if (returnFocusTarget?.isConnected) {
      returnFocusTarget.focus({ preventScroll: true })
    }
    returnFocusTarget = null
  })
}

export function showManualCopy(text: string, label: string): boolean {
  initializeManualCopy()
  if (!elements) {
    showMessage('自动复制失败，手动复制窗口暂时不可用。', 'danger')
    return false
  }

  const modal = getBootstrapModal(elements.modal)
  if (!modal) {
    showMessage('自动复制失败，手动复制窗口暂时不可用。', 'danger')
    return false
  }

  returnFocusTarget = document.activeElement instanceof HTMLElement
    ? document.activeElement
    : null
  elements.label.textContent = label
  elements.textArea.value = text
  modal.show()
  showMessage('自动复制失败，请在窗口中手动复制。', 'warning')
  return true
}
