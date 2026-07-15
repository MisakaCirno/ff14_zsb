export type CopyTextResult =
  | { status: 'copied'; method: 'clipboard' | 'fallback' }
  | { status: 'manual-required' }

const clipboardWriteTimeoutMs = 1500

async function copyWithClipboardApi(text: string): Promise<boolean> {
  if (!navigator.clipboard?.writeText) {
    return false
  }

  let timeoutId: number | undefined
  try {
    const copied = await Promise.race([
      navigator.clipboard.writeText(text).then(() => true),
      new Promise<boolean>((resolve) => {
        timeoutId = window.setTimeout(
          () => resolve(false),
          clipboardWriteTimeoutMs,
        )
      }),
    ])
    if (!copied) {
      console.warn('Clipboard API timed out; using the fallback.')
    }
    return copied
  } catch (error) {
    console.warn('Clipboard API failed; using the fallback.', error)
    return false
  } finally {
    if (timeoutId !== undefined) {
      window.clearTimeout(timeoutId)
    }
  }
}

function fallbackCopyTextToClipboard(
  text: string,
): boolean {
  const textArea = document.createElement('textarea')
  const previouslyFocused = document.activeElement
  textArea.value = text
  textArea.readOnly = true
  textArea.tabIndex = -1
  textArea.setAttribute('aria-hidden', 'true')
  textArea.style.position = 'fixed'
  textArea.style.inset = '0 auto auto 0'
  textArea.style.width = '1px'
  textArea.style.height = '1px'
  textArea.style.opacity = '0'
  textArea.style.pointerEvents = 'none'
  document.body.appendChild(textArea)
  textArea.focus()
  textArea.select()

  let copied = false
  try {
    copied = document.execCommand('copy')
  } catch (error) {
    console.warn('Clipboard fallback failed.', error)
  } finally {
    textArea.remove()
    if (previouslyFocused instanceof HTMLElement && previouslyFocused.isConnected) {
      previouslyFocused.focus({ preventScroll: true })
    }
  }

  return copied
}

export async function copyText(text: string): Promise<CopyTextResult> {
  if (await copyWithClipboardApi(text)) {
    return { status: 'copied', method: 'clipboard' }
  }

  if (fallbackCopyTextToClipboard(text)) {
    return { status: 'copied', method: 'fallback' }
  }

  return { status: 'manual-required' }
}
