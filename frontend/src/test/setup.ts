import { afterEach, vi } from 'vitest'

afterEach(() => {
  document.body.replaceChildren()
  document.head.replaceChildren()
  localStorage.clear()
  sessionStorage.clear()
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: undefined,
  })
  Object.defineProperty(document, 'execCommand', {
    configurable: true,
    value: undefined,
  })
  delete window.bootstrap
  delete window.QRCode
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.resetModules()
})
