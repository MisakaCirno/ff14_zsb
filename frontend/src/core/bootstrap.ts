export interface BootstrapModal {
  hide(): void
  show(): void
}

interface BootstrapNamespace {
  Modal: {
    getOrCreateInstance(element: Element): BootstrapModal
  }
}

declare global {
  interface Window {
    bootstrap?: BootstrapNamespace
  }
}

export function getBootstrapModal(element: Element): BootstrapModal | null {
  const modalApi = window.bootstrap?.Modal
  return modalApi ? modalApi.getOrCreateInstance(element) : null
}
