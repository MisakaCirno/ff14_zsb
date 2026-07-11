import { showMessage } from './notify'

type CopySuccess = () => void

function fallbackCopyTextToClipboard(
  text: string,
  onSuccess?: CopySuccess,
): void {
  const textArea = document.createElement('textarea')
  textArea.value = text
  textArea.style.position = 'fixed'
  textArea.style.inset = '0 auto auto 0'
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
  }

  if (copied) {
    onSuccess?.()
  } else {
    showMessage(`复制失败，请手动复制：${text}`, 'warning')
  }
}

export async function copyText(text: string, onSuccess?: CopySuccess): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      onSuccess?.()
      return
    } catch (error) {
      console.warn('Clipboard API failed; using the fallback.', error)
    }
  }

  fallbackCopyTextToClipboard(text, onSuccess)
}
