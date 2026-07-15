import { describe, expect, it, vi } from 'vitest'

function renderGuardedShareDetail(): {
  generateButton: HTMLButtonElement
  overlay: HTMLElement
  preview: HTMLImageElement
  revealButton: HTMLButtonElement
  root: HTMLElement
} {
  document.body.innerHTML = `
    <main data-share-detail data-content-revealed="false">
      <section data-content-overlay>
        <button type="button" aria-expanded="false" data-reveal-content>
          <span data-reveal-label>查看内容</span>
        </button>
      </section>
      <img class="blur-content" aria-hidden="true" data-share-preview>
      <button type="button" aria-disabled="true" disabled data-generate-share-image>
        生成分享图
      </button>
      <span class="d-none" data-share-image-spinner></span>
    </main>
    <div data-share-image-modal>
      <canvas data-share-image-canvas></canvas>
      <button type="button" data-copy-share-image>复制图片</button>
      <button type="button" data-download-share-image>下载图片</button>
    </div>
  `
  return {
    generateButton: document.querySelector<HTMLButtonElement>('[data-generate-share-image]')!,
    overlay: document.querySelector<HTMLElement>('[data-content-overlay]')!,
    preview: document.querySelector<HTMLImageElement>('[data-share-preview]')!,
    revealButton: document.querySelector<HTMLButtonElement>('[data-reveal-content]')!,
    root: document.querySelector<HTMLElement>('[data-share-detail]')!,
  }
}

describe('share detail content reveal', () => {
  it('synchronizes accessibility state and unlocks share-image generation', async () => {
    const elements = renderGuardedShareDetail()
    const revealed = vi.fn()
    elements.root.addEventListener('share:content-revealed', revealed)
    const { initializeShareDetails } = await import('./share-detail')
    const { initializeShareImages } = await import('./share-image')
    initializeShareDetails()
    initializeShareImages()

    expect(elements.generateButton.disabled).toBe(true)
    expect(elements.generateButton.getAttribute('aria-disabled')).toBe('true')
    document.querySelector<HTMLElement>('[data-reveal-label]')!.click()

    expect(elements.root.dataset.contentRevealed).toBe('true')
    expect(elements.revealButton.getAttribute('aria-expanded')).toBe('true')
    expect(elements.overlay.hidden).toBe(true)
    expect(elements.preview.classList.contains('blur-content')).toBe(false)
    expect(elements.preview.hasAttribute('aria-hidden')).toBe(false)
    expect(elements.preview.tabIndex).toBe(-1)
    expect(document.activeElement).toBe(elements.preview)
    expect(revealed).toHaveBeenCalledOnce()
    expect(elements.generateButton.disabled).toBe(false)
    expect(elements.generateButton.getAttribute('aria-disabled')).toBe('false')

    elements.revealButton.click()
    expect(revealed).toHaveBeenCalledOnce()
  })
})
