import { performShareCopy } from './share-copy'

function copyStrategyCode(button: HTMLButtonElement): void {
  const code = button.dataset.copyCode
  if (code === undefined) {
    return
  }

  const compactFeedback = (button.textContent ?? '').trim() === ''
  void performShareCopy({
    button,
    compactFeedback,
    manualLabel: '战术板代码',
    recordUrl: button.dataset.recordCopyUrl,
    successMessage: '战术板代码已复制',
    text: code,
  })
}

function copyQqGroup(button: HTMLButtonElement): void {
  const qqGroup = button.dataset.copyQq
  if (!qqGroup) {
    return
  }
  void performShareCopy({
    button,
    manualLabel: 'QQ群号',
    successMessage: `QQ群号 ${qqGroup} 已复制到剪贴板！`,
    text: qqGroup,
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
