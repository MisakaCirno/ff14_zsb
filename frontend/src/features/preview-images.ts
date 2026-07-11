function hidePreviewLoading(image: HTMLImageElement): void {
  image
    .closest<HTMLElement>('[data-preview-frame]')
    ?.querySelector<HTMLElement>('[data-preview-loading]')
    ?.classList.add('d-none')
}

const initializedImages = new WeakSet<HTMLImageElement>()

function initializePreviewImage(image: HTMLImageElement): void {
  if (initializedImages.has(image)) {
    return
  }
  initializedImages.add(image)
  image.addEventListener('load', () => hidePreviewLoading(image), { once: true })
  image.addEventListener('error', () => hidePreviewLoading(image), { once: true })
  if (image.complete) {
    hidePreviewLoading(image)
  }
}

function initializePreviewImagesWithin(root: ParentNode): void {
  if (root instanceof HTMLImageElement && root.matches('[data-preview-image]')) {
    initializePreviewImage(root)
  }
  root.querySelectorAll<HTMLImageElement>('[data-preview-image]').forEach(initializePreviewImage)
}

export function initializePreviewImages(): void {
  initializePreviewImagesWithin(document)
  document.addEventListener('htmx:load', (event) => {
    const element = (event as CustomEvent<{ elt?: Element }>).detail?.elt
    if (element) {
      initializePreviewImagesWithin(element)
    }
  })
}
