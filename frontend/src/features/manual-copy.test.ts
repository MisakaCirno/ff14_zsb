import { describe, expect, it, vi } from 'vitest'

function renderManualCopyModal(): {
  label: HTMLElement
  modal: HTMLElement
  selectButton: HTMLButtonElement
  textArea: HTMLTextAreaElement
} {
  document.body.innerHTML = `
    <button type="button" data-return-focus>复制</button>
    <div id="message-container"></div>
    <div data-manual-copy-modal>
      <h2 data-manual-copy-label>待复制内容</h2>
      <textarea data-manual-copy-text></textarea>
      <button type="button" data-manual-copy-select>全选</button>
    </div>
  `
  return {
    label: document.querySelector<HTMLElement>('[data-manual-copy-label]')!,
    modal: document.querySelector<HTMLElement>('[data-manual-copy-modal]')!,
    selectButton: document.querySelector<HTMLButtonElement>('[data-manual-copy-select]')!,
    textArea: document.querySelector<HTMLTextAreaElement>('[data-manual-copy-text]')!,
  }
}

describe('manual copy modal', () => {
  it('selects content, clears state, and restores the invoking control focus', async () => {
    vi.useFakeTimers()
    const elements = renderManualCopyModal()
    const returnFocus = document.querySelector<HTMLButtonElement>('[data-return-focus]')!
    const show = vi.fn()
    const getOrCreateInstance = vi.fn(() => ({ hide: vi.fn(), show }))
    window.bootstrap = { Modal: { getOrCreateInstance } }
    returnFocus.focus()
    const { showManualCopy } = await import('./manual-copy')

    expect(showManualCopy('需要手动复制的内容', '战术板代码')).toBe(true)
    expect(getOrCreateInstance).toHaveBeenCalledWith(elements.modal)
    expect(show).toHaveBeenCalledOnce()
    expect(elements.label.textContent).toBe('战术板代码')
    expect(elements.textArea.value).toBe('需要手动复制的内容')

    elements.modal.dispatchEvent(new Event('shown.bs.modal'))
    expect(document.activeElement).toBe(elements.textArea)
    expect(elements.textArea.selectionStart).toBe(0)
    expect(elements.textArea.selectionEnd).toBe(elements.textArea.value.length)

    elements.selectButton.focus()
    elements.selectButton.click()
    expect(document.activeElement).toBe(elements.textArea)
    expect(elements.textArea.selectionEnd).toBe(elements.textArea.value.length)

    elements.modal.dispatchEvent(new Event('hidden.bs.modal'))
    expect(elements.textArea.value).toBe('')
    expect(elements.label.textContent).not.toBe('战术板代码')
    expect(document.activeElement).toBe(returnFocus)
  })

  it('shows an accessible danger notification when Bootstrap is unavailable', async () => {
    vi.useFakeTimers()
    renderManualCopyModal()
    const { showManualCopy } = await import('./manual-copy')

    expect(showManualCopy('content', 'label')).toBe(false)
    const alert = document.querySelector<HTMLElement>('[data-notification]')
    expect(alert).not.toBeNull()
    expect(alert?.getAttribute('role')).toBe('alert')
    expect(alert?.classList.contains('alert-danger')).toBe(true)
    expect(alert?.textContent?.trim().length).toBeGreaterThan(0)
  })
})
