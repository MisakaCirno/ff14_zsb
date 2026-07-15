import { describe, expect, it, vi } from 'vitest'

function stubClipboardWrite(writeText: (text: string) => Promise<void>): void {
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: { writeText: vi.fn(writeText) },
  })
}

function stubExecCommand(copy: () => boolean): ReturnType<typeof vi.fn> {
  const execCommand = vi.fn(copy)
  Object.defineProperty(document, 'execCommand', {
    configurable: true,
    value: execCommand,
  })
  return execCommand
}

describe('copyText', () => {
  it('uses the Clipboard API when it succeeds', async () => {
    stubClipboardWrite(async () => undefined)
    const execCommand = stubExecCommand(() => true)
    const { copyText } = await import('./clipboard')

    await expect(copyText('strategy code')).resolves.toEqual({
      method: 'clipboard',
      status: 'copied',
    })
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('strategy code')
    expect(execCommand).not.toHaveBeenCalled()
  })

  it('falls back when the Clipboard API rejects', async () => {
    const failure = new DOMException('Permission denied', 'NotAllowedError')
    stubClipboardWrite(async () => Promise.reject(failure))
    const execCommand = stubExecCommand(() => true)
    vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const { copyText } = await import('./clipboard')

    await expect(copyText('fallback value')).resolves.toEqual({
      method: 'fallback',
      status: 'copied',
    })
    expect(execCommand).toHaveBeenCalledWith('copy')
    expect(console.warn).toHaveBeenCalledWith(
      'Clipboard API failed; using the fallback.',
      failure,
    )
  })

  it('times out a permanently pending Clipboard API call', async () => {
    vi.useFakeTimers()
    stubClipboardWrite(() => new Promise<void>(() => undefined))
    stubExecCommand(() => true)
    vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const { copyText } = await import('./clipboard')

    const result = copyText('pending value')
    await vi.advanceTimersByTimeAsync(1500)

    await expect(result).resolves.toEqual({
      method: 'fallback',
      status: 'copied',
    })
    expect(console.warn).toHaveBeenCalledWith(
      'Clipboard API timed out; using the fallback.',
    )
  })

  it('removes the fallback textarea and restores focus after execCommand succeeds', async () => {
    const focusTarget = document.createElement('button')
    document.body.appendChild(focusTarget)
    focusTarget.focus()
    const execCommand = stubExecCommand(() => {
      const textArea = document.querySelector<HTMLTextAreaElement>('textarea')
      expect(textArea?.value).toBe('legacy copy')
      expect(document.activeElement).toBe(textArea)
      return true
    })
    const { copyText } = await import('./clipboard')

    await expect(copyText('legacy copy')).resolves.toEqual({
      method: 'fallback',
      status: 'copied',
    })
    expect(execCommand).toHaveBeenCalledWith('copy')
    expect(document.querySelector('textarea')).toBeNull()
    expect(document.activeElement).toBe(focusTarget)
  })

  it('cleans up and requests manual copy after execCommand fails', async () => {
    const focusTarget = document.createElement('input')
    document.body.appendChild(focusTarget)
    focusTarget.focus()
    const execCommand = stubExecCommand(() => false)
    const { copyText } = await import('./clipboard')

    await expect(copyText('manual copy')).resolves.toEqual({
      status: 'manual-required',
    })
    expect(execCommand).toHaveBeenCalledWith('copy')
    expect(document.querySelector('textarea')).toBeNull()
    expect(document.activeElement).toBe(focusTarget)
  })
})
