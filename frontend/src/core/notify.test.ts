import { describe, expect, it, vi } from 'vitest'

function renderNotificationContainer(): HTMLElement {
  document.body.innerHTML = `
    <button type="button" data-invoker>执行操作</button>
    <div id="message-container" role="region" aria-live="polite"></div>
    <main id="main-content" tabindex="-1"></main>
  `
  return document.getElementById('message-container')!
}

describe('showMessage', () => {
  it.each(['danger', 'warning'])('%s notifications wait for the user to close them', async (type) => {
    vi.useFakeTimers()
    renderNotificationContainer()
    const { showMessage } = await import('./notify')

    showMessage('需要处理的消息', type)
    const alert = document.querySelector<HTMLElement>('[data-notification]')!

    expect(alert.hasAttribute('data-notification-persistent')).toBe(true)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(alert.isConnected).toBe(true)

    alert.querySelector<HTMLButtonElement>('.btn-close')!.click()
    await vi.advanceTimersByTimeAsync(150)
    expect(alert.isConnected).toBe(false)
  })

  it('automatically removes an ordinary notification after its reading time', async () => {
    vi.useFakeTimers()
    renderNotificationContainer()
    const { showMessage } = await import('./notify')

    showMessage('保存成功', 'success')
    const alert = document.querySelector<HTMLElement>('[data-notification]')!

    await vi.advanceTimersByTimeAsync(2999)
    expect(alert.isConnected).toBe(true)
    await vi.advanceTimersByTimeAsync(151)
    expect(alert.isConnected).toBe(false)
  })

  it('does not auto-remove a notification while one of its controls has focus', async () => {
    vi.useFakeTimers()
    const container = renderNotificationContainer()
    const outsideButton = document.querySelector<HTMLButtonElement>('[data-invoker]')!
    const { showMessage } = await import('./notify')

    showMessage('保存成功', 'success')
    const alert = container.querySelector<HTMLElement>('[data-notification]')!
    const closeButton = alert.querySelector<HTMLButtonElement>('.btn-close')!
    closeButton.focus()

    await vi.advanceTimersByTimeAsync(5000)
    expect(alert.isConnected).toBe(true)
    expect(document.activeElement).toBe(closeButton)

    outsideButton.focus()
    await Promise.resolve()
    await vi.advanceTimersByTimeAsync(150)
    expect(alert.isConnected).toBe(false)
    expect(document.activeElement).toBe(outsideButton)
  })

  it('restores the invoking control focus after a focused close button removes the notification', async () => {
    vi.useFakeTimers()
    renderNotificationContainer()
    const invoker = document.querySelector<HTMLButtonElement>('[data-invoker]')!
    invoker.focus()
    const { showMessage } = await import('./notify')

    showMessage('保存成功', 'success')
    const alert = document.querySelector<HTMLElement>('[data-notification]')!
    const closeButton = alert.querySelector<HTMLButtonElement>('.btn-close')!
    closeButton.focus()
    closeButton.click()
    await vi.advanceTimersByTimeAsync(150)

    expect(alert.isConnected).toBe(false)
    expect(document.activeElement).toBe(invoker)
  })

  it('does not steal focus when the user moves elsewhere during the removal transition', async () => {
    vi.useFakeTimers()
    renderNotificationContainer()
    const invoker = document.querySelector<HTMLButtonElement>('[data-invoker]')!
    const main = document.getElementById('main-content')!
    invoker.focus()
    const { showMessage } = await import('./notify')

    showMessage('保存成功', 'success')
    const alert = document.querySelector<HTMLElement>('[data-notification]')!
    const closeButton = alert.querySelector<HTMLButtonElement>('.btn-close')!
    closeButton.focus()
    closeButton.click()
    await vi.advanceTimersByTimeAsync(100)
    main.focus()
    await vi.advanceTimersByTimeAsync(50)

    expect(alert.isConnected).toBe(false)
    expect(document.activeElement).toBe(main)
  })

  it('does not reclaim focus after the user deliberately blurs the close button', async () => {
    vi.useFakeTimers()
    renderNotificationContainer()
    const invoker = document.querySelector<HTMLButtonElement>('[data-invoker]')!
    invoker.focus()
    const { showMessage } = await import('./notify')

    showMessage('保存成功', 'success')
    const alert = document.querySelector<HTMLElement>('[data-notification]')!
    const closeButton = alert.querySelector<HTMLButtonElement>('.btn-close')!
    closeButton.focus()
    closeButton.click()
    closeButton.blur()

    expect(document.activeElement).toBe(document.body)
    await vi.advanceTimersByTimeAsync(150)

    expect(alert.isConnected).toBe(false)
    expect(document.activeElement).toBe(document.body)
  })

  it('uses the main content as the focus fallback when the invoker no longer exists', async () => {
    vi.useFakeTimers()
    renderNotificationContainer()
    const invoker = document.querySelector<HTMLButtonElement>('[data-invoker]')!
    const main = document.getElementById('main-content')!
    invoker.focus()
    const { showMessage } = await import('./notify')

    showMessage('保存成功', 'success')
    const closeButton = document.querySelector<HTMLButtonElement>('[data-notification] .btn-close')!
    invoker.remove()
    closeButton.focus()
    closeButton.click()
    await vi.advanceTimersByTimeAsync(150)

    expect(document.activeElement).toBe(main)
  })
})
