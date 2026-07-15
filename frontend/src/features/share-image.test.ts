import { afterEach, describe, expect, it, vi } from 'vitest'

import { fitCanvasText, initializeShareImages } from './share-image'

function createContext(measureWidth: (text: string) => number): CanvasRenderingContext2D {
  return {
    measureText: vi.fn((text: string) => ({ width: measureWidth(text) })),
  } as unknown as CanvasRenderingContext2D
}

function codePointWidth(text: string): number {
  return Array.from(text).length
}

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.replaceChildren()
})

describe('fitCanvasText', () => {
  it('returns text unchanged when it already fits', () => {
    const context = createContext(codePointWidth)

    expect(fitCanvasText(context, '四人本攻略', 5)).toBe('四人本攻略')
  })

  it('uses binary search to truncate ordinary text', () => {
    const context = createContext(codePointWidth)

    expect(fitCanvasText(context, 'abcdef', 4)).toBe('abc…')
  })

  it.each([0, -1, Number.NaN, Number.POSITIVE_INFINITY])(
    'returns an empty string for invalid width %s',
    (maxWidth) => {
      const context = createContext(codePointWidth)

      expect(fitCanvasText(context, 'text', maxWidth)).toBe('')
    },
  )

  it('returns an empty string when even the ellipsis cannot fit', () => {
    const context = createContext(codePointWidth)

    expect(fitCanvasText(context, 'abcdef', 0.5)).toBe('')
  })

  it('does not split a family ZWJ emoji', () => {
    const context = createContext(codePointWidth)

    expect(fitCanvasText(context, 'A👨‍👩‍👧‍👦B', 3)).toBe('A…')
  })

  it('does not split an emoji from its skin-tone modifier', () => {
    const context = createContext(codePointWidth)

    expect(fitCanvasText(context, '👍🏽X', 2)).toBe('…')
  })

  it('does not split a combining accent from its base character', () => {
    const context = createContext(codePointWidth)

    expect(fitCanvasText(context, 'e\u0301X', 2)).toBe('…')
  })

  it('falls back to Unicode code points when Intl.Segmenter is unavailable', () => {
    const originalIntl = Intl
    vi.stubGlobal('Intl', { ...originalIntl, Segmenter: undefined })
    const context = createContext(codePointWidth)

    expect(fitCanvasText(context, '😀AB', 2)).toBe('😀…')
  })

  it('falls back when Intl.Segmenter construction fails', () => {
    class BrokenSegmenter {
      constructor() {
        throw new Error('construction failed')
      }
    }
    vi.stubGlobal('Intl', { Segmenter: BrokenSegmenter })
    const context = createContext(codePointWidth)

    expect(fitCanvasText(context, '😀AB', 2)).toBe('😀…')
  })

  it('falls back when grapheme segmentation fails', () => {
    class BrokenSegmenter {
      segment(): never {
        throw new Error('segmentation failed')
      }
    }
    vi.stubGlobal('Intl', { Segmenter: BrokenSegmenter })
    const context = createContext(codePointWidth)

    expect(fitCanvasText(context, '😀AB', 2)).toBe('😀…')
  })
})

describe('initializeShareImages', () => {
  it('returns focus to the generate button after the preview modal closes', () => {
    document.body.innerHTML = `
      <main data-share-detail data-content-revealed="true">
        <img data-share-preview alt="">
        <button type="button" data-generate-share-image>生成分享图</button>
        <span data-share-image-spinner></span>
      </main>
      <div data-share-image-modal>
        <canvas data-share-image-canvas></canvas>
        <button type="button" data-copy-share-image>复制</button>
        <button type="button" data-download-share-image>下载</button>
      </div>
      <button type="button" id="outside">其他操作</button>
    `
    initializeShareImages()
    const generateButton = document.querySelector<HTMLButtonElement>(
      '[data-generate-share-image]',
    )
    const modal = document.querySelector<HTMLElement>('[data-share-image-modal]')
    const outsideButton = document.querySelector<HTMLButtonElement>('#outside')
    expect(generateButton).not.toBeNull()
    expect(modal).not.toBeNull()
    expect(outsideButton).not.toBeNull()

    outsideButton?.focus()
    modal?.dispatchEvent(new Event('hidden.bs.modal'))

    expect(document.activeElement).toBe(generateButton)
  })
})
