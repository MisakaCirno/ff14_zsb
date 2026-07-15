import { getBootstrapModal } from '../core/bootstrap'
import { showMessage } from '../core/notify'
import {
  restoreActionButton,
  scheduleActionButtonRestore,
  setActionButtonBusy,
  setActionButtonSuccess,
  snapshotActionButton,
} from './share-copy'

interface QrCodeOptions {
  colorDark: string
  colorLight: string
  correctLevel: number
  height: number
  text: string
  width: number
}

interface QrCodeConstructor {
  new (element: HTMLElement, options: QrCodeOptions): object
  readonly CorrectLevel: {
    readonly H: number
  }
}

declare global {
  interface Window {
    QRCode?: QrCodeConstructor
  }
}

interface ShareImageElements {
  canvas: HTMLCanvasElement
  copyButton: HTMLButtonElement
  downloadButton: HTMLButtonElement
  generateButton: HTMLButtonElement
  modal: HTMLElement
  previewImage: HTMLImageElement
  root: HTMLElement
  spinner: HTMLElement
}

const targetWidth = 960
const targetHeight = 720
const headerHeight = 72

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '未知错误'
}

function loadImage(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image()
    const cleanup = (): void => {
      window.clearTimeout(timeout)
      image.onload = null
      image.onerror = null
    }
    const timeout = window.setTimeout(() => {
      cleanup()
      reject(new Error('图片加载超时'))
    }, 10000)

    image.onload = () => {
      cleanup()
      resolve(image)
    }
    image.onerror = () => {
      cleanup()
      reject(new Error('图片加载失败'))
    }
    image.crossOrigin = 'anonymous'
    image.src = url
  })
}

function waitForImage(image: HTMLImageElement): Promise<void> {
  if (image.complete && image.naturalWidth > 0) {
    return Promise.resolve()
  }

  return new Promise((resolve, reject) => {
    const cleanup = (): void => {
      window.clearTimeout(timeout)
      image.onload = null
      image.onerror = null
    }
    const timeout = window.setTimeout(() => {
      cleanup()
      reject(new Error('二维码加载超时'))
    }, 1000)
    image.onload = () => {
      cleanup()
      resolve()
    }
    image.onerror = () => {
      cleanup()
      reject(new Error('二维码加载失败'))
    }
  })
}

function calculateRegionComplexity(
  context: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
): number {
  try {
    const data = context.getImageData(x, y, width, height).data
    let score = 0
    for (let index = 0; index < data.length; index += 16) {
      const firstLuminance = 0.299 * (data[index] ?? 0)
        + 0.587 * (data[index + 1] ?? 0)
        + 0.114 * (data[index + 2] ?? 0)
      if (index + 4 < data.length) {
        const secondLuminance = 0.299 * (data[index + 4] ?? 0)
          + 0.587 * (data[index + 5] ?? 0)
          + 0.114 * (data[index + 6] ?? 0)
        score += Math.abs(firstLuminance - secondLuminance)
      }
    }
    return score
  } catch (error) {
    console.warn('Unable to calculate QR placement complexity.', error)
    return 0
  }
}

function createQrContainer(cleanUrl: string): HTMLElement {
  const QrCode = window.QRCode
  if (typeof QrCode !== 'function') {
    throw new Error('二维码组件未加载')
  }

  const container = document.createElement('div')
  container.style.position = 'absolute'
  container.style.inset = 'auto auto auto -9999px'
  container.style.visibility = 'hidden'
  document.body.appendChild(container)
  try {
    new QrCode(container, {
      text: cleanUrl,
      width: 180,
      height: 180,
      colorDark: '#000000',
      colorLight: '#ffffff',
      correctLevel: QrCode.CorrectLevel.H,
    })
  } catch (error) {
    container.remove()
    throw error
  }
  return container
}

async function getQrImage(container: HTMLElement): Promise<CanvasImageSource> {
  const canvas = container.querySelector<HTMLCanvasElement>('canvas')
  if (canvas) {
    return canvas
  }

  const image = container.querySelector<HTMLImageElement>('img')
  if (!image) {
    throw new Error('二维码生成失败')
  }
  await waitForImage(image)
  return image
}

function drawQrCode(
  context: CanvasRenderingContext2D,
  canvas: HTMLCanvasElement,
  qrImage: CanvasImageSource,
): { x: number; y: number; size: number } {
  const size = 120
  const padding = 15
  const borderRadius = 6
  const checkSize = size + padding * 2
  const bottom = canvas.height - checkSize
  const leftScore = calculateRegionComplexity(context, 0, bottom, checkSize, checkSize)
  const rightScore = calculateRegionComplexity(
    context,
    canvas.width - checkSize,
    bottom,
    checkSize,
    checkSize,
  )
  const x = rightScore < leftScore ? canvas.width - size - padding : padding
  const y = canvas.height - size - padding

  context.fillStyle = 'rgba(255, 255, 255, 0.98)'
  context.shadowColor = 'rgba(0, 0, 0, 0.3)'
  context.shadowBlur = 8
  context.shadowOffsetX = 0
  context.shadowOffsetY = 3
  context.beginPath()
  context.moveTo(x + borderRadius, y)
  context.lineTo(x + size - borderRadius, y)
  context.quadraticCurveTo(x + size, y, x + size, y + borderRadius)
  context.lineTo(x + size, y + size - borderRadius)
  context.quadraticCurveTo(x + size, y + size, x + size - borderRadius, y + size)
  context.lineTo(x + borderRadius, y + size)
  context.quadraticCurveTo(x, y + size, x, y + size - borderRadius)
  context.lineTo(x, y + borderRadius)
  context.quadraticCurveTo(x, y, x + borderRadius, y)
  context.closePath()
  context.fill()
  context.shadowColor = 'transparent'
  context.shadowBlur = 0
  context.shadowOffsetX = 0
  context.shadowOffsetY = 0
  context.strokeStyle = '#0d6efd'
  context.lineWidth = 2
  context.stroke()

  const qrPadding = 8
  context.drawImage(
    qrImage,
    x + qrPadding,
    y + qrPadding,
    size - qrPadding * 2,
    size - qrPadding * 2,
  )
  return { x, y, size }
}

export function fitCanvasText(
  context: CanvasRenderingContext2D,
  text: string,
  maxWidth: number,
): string {
  if (!Number.isFinite(maxWidth) || maxWidth <= 0) {
    return ''
  }
  if (context.measureText(text).width <= maxWidth) {
    return text
  }

  const ellipsis = '…'
  if (context.measureText(ellipsis).width > maxWidth) {
    return ''
  }

  let characters = Array.from(text)
  if (typeof Intl.Segmenter === 'function') {
    try {
      const segmenter = new Intl.Segmenter(undefined, { granularity: 'grapheme' })
      characters = Array.from(segmenter.segment(text), ({ segment }) => segment)
    } catch {
      // Keep the code-point fallback for incomplete or faulty implementations.
    }
  }
  let low = 0
  let high = characters.length
  while (low < high) {
    const middle = Math.ceil((low + high) / 2)
    const candidate = `${characters.slice(0, middle).join('')}${ellipsis}`
    if (context.measureText(candidate).width <= maxWidth) {
      low = middle
    } else {
      high = middle - 1
    }
  }
  return `${characters.slice(0, low).join('')}${ellipsis}`
}

function drawHeader(
  context: CanvasRenderingContext2D,
  canvas: HTMLCanvasElement,
  cleanUrl: string,
  title: string,
  author: string,
): void {
  const horizontalPadding = 20
  const columnGap = 24
  const secondaryAvailableWidth = canvas.width - horizontalPadding * 2 - columnGap
  const authorMaxWidth = Math.floor(secondaryAvailableWidth * 0.4)
  const urlMaxWidth = secondaryAvailableWidth - authorMaxWidth

  context.save()
  const gradient = context.createLinearGradient(0, 0, canvas.width, 0)
  gradient.addColorStop(0, 'rgba(13, 110, 253, 0.95)')
  gradient.addColorStop(1, 'rgba(102, 126, 234, 0.95)')
  context.fillStyle = gradient
  context.fillRect(0, 0, canvas.width, headerHeight)

  context.fillStyle = '#ffffff'
  context.textAlign = 'left'
  context.font = 'bold 20px "Microsoft YaHei", Arial, sans-serif'
  context.fillText(
    fitCanvasText(context, title, canvas.width - horizontalPadding * 2),
    horizontalPadding,
    27,
  )

  context.font = '15px "Microsoft YaHei", Arial, sans-serif'
  context.fillStyle = 'rgba(255, 255, 255, 0.9)'
  context.fillText(
    fitCanvasText(context, `作者：${author}`, authorMaxWidth),
    horizontalPadding,
    56,
  )

  context.textAlign = 'right'
  context.font = '14px "Microsoft YaHei", Arial, sans-serif'
  context.fillStyle = 'rgba(255, 255, 255, 0.8)'
  context.fillText(
    fitCanvasText(context, cleanUrl.replace(/^https?:\/\//, ''), urlMaxWidth),
    canvas.width - horizontalPadding,
    56,
  )
  context.restore()
}

function drawQrLabel(
  context: CanvasRenderingContext2D,
  position: { x: number; y: number; size: number },
): void {
  const labelX = position.x + position.size / 2
  const labelY = position.y - 6
  context.font = 'bold 14px "Microsoft YaHei", Arial, sans-serif'
  context.textAlign = 'center'
  context.strokeStyle = '#ffffff'
  context.lineWidth = 3
  context.strokeText('获取代码', labelX, labelY)
  context.fillStyle = '#495057'
  context.fillText('获取代码', labelX, labelY)
}

function showShareImageModal(modalElement: HTMLElement): void {
  const modal = getBootstrapModal(modalElement)
  if (!modal) {
    throw new Error('分享图预览组件未加载')
  }
  modal.show()
}

function isContentRevealed(root: HTMLElement): boolean {
  return root.dataset.contentRevealed === 'true'
}

function synchronizeGenerateAvailability(elements: ShareImageElements): void {
  if (elements.generateButton.getAttribute('aria-busy') === 'true') {
    return
  }
  const revealed = isContentRevealed(elements.root)
  elements.generateButton.disabled = !revealed
  elements.generateButton.setAttribute('aria-disabled', String(!revealed))
}

async function generateShareImage(elements: ShareImageElements): Promise<void> {
  if (!isContentRevealed(elements.root)) {
    synchronizeGenerateAvailability(elements)
    showMessage('请先确认内容警告并查看预览，再生成分享图。', 'warning')
    return
  }

  const buttonSnapshot = snapshotActionButton(elements.generateButton)
  setActionButtonBusy(elements.generateButton, '生成中...')
  elements.spinner.classList.remove('d-none')
  let qrContainer: HTMLElement | null = null

  try {
    const previewUrl = elements.previewImage.currentSrc || elements.previewImage.src
    const previewImage = await loadImage(previewUrl)
    const previewWidth = previewImage.naturalWidth || previewImage.width
    const previewHeight = previewImage.naturalHeight || previewImage.height
    if (previewWidth <= 0 || previewHeight <= 0) {
      throw new Error('预览图尺寸无效')
    }

    elements.canvas.width = targetWidth
    elements.canvas.height = targetHeight
    const context = elements.canvas.getContext('2d')
    if (!context) {
      throw new Error('浏览器不支持 Canvas 2D')
    }
    context.fillStyle = '#ffffff'
    context.fillRect(0, 0, targetWidth, targetHeight)
    const scale = Math.max(targetWidth / previewWidth, targetHeight / previewHeight)
    const xOffset = (targetWidth - previewWidth * scale) / 2
    const yOffset = (targetHeight - previewHeight * scale) / 2
    context.drawImage(
      previewImage,
      xOffset,
      yOffset,
      previewWidth * scale,
      previewHeight * scale,
    )

    const cleanUrl = elements.root.dataset.shareUrl
    if (!cleanUrl) {
      throw new Error('分享链接缺失')
    }
    qrContainer = createQrContainer(cleanUrl)
    const qrImage = await getQrImage(qrContainer)
    const qrPosition = drawQrCode(context, elements.canvas, qrImage)
    drawHeader(
      context,
      elements.canvas,
      cleanUrl,
      elements.root.dataset.shareTitle || '未命名分享',
      elements.root.dataset.shareAuthor || '匿名用户',
    )
    drawQrLabel(context, qrPosition)
    showShareImageModal(elements.modal)
    window.setTimeout(() => showMessage('分享图生成成功！', 'success'), 100)
  } catch (error) {
    console.error('Unable to generate the share image.', error)
    showMessage(`生成分享图失败：${errorMessage(error)}`, 'danger')
  } finally {
    restoreActionButton(elements.generateButton, buttonSnapshot)
    synchronizeGenerateAvailability(elements)
    elements.spinner.classList.add('d-none')
    qrContainer?.remove()
  }
}

function canvasToBlob(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob === null) {
        reject(new Error('无法生成 PNG 图片'))
        return
      }
      resolve(blob)
    }, 'image/png')
  })
}

async function copyShareImage(elements: ShareImageElements): Promise<void> {
  const buttonSnapshot = snapshotActionButton(elements.copyButton)
  setActionButtonBusy(
    elements.copyButton,
    '复制中...',
    'spinner-border spinner-border-sm',
  )

  try {
    const blob = await canvasToBlob(elements.canvas)
    if (navigator.clipboard?.write && typeof ClipboardItem !== 'undefined') {
      await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })])
      setActionButtonSuccess(elements.copyButton, '已复制')
      scheduleActionButtonRestore(elements.copyButton, buttonSnapshot)
      showMessage('分享图已复制到剪贴板。', 'success')
      return
    }

    restoreActionButton(elements.copyButton, buttonSnapshot)
    showMessage('当前浏览器不支持直接复制图片，请使用“下载图片”。', 'warning')
  } catch (error) {
    console.error('Unable to copy the share image.', error)
    restoreActionButton(elements.copyButton, buttonSnapshot)
    showMessage('复制失败，请尝试使用“下载图片”功能，或右键保存图片。', 'danger')
  }
}

async function downloadShareImage(elements: ShareImageElements): Promise<void> {
  const buttonSnapshot = snapshotActionButton(elements.downloadButton)
  setActionButtonBusy(
    elements.downloadButton,
    '下载中...',
    'spinner-border spinner-border-sm',
  )
  let objectUrl: string | null = null
  let link: HTMLAnchorElement | null = null
  try {
    const shareId = (elements.root.dataset.shareId || 'share')
      .replace(/[^a-zA-Z0-9_-]/g, '') || 'share'
    const blob = await canvasToBlob(elements.canvas)
    objectUrl = URL.createObjectURL(blob)
    link = document.createElement('a')
    link.download = `zsb-share-${shareId}-${Date.now()}.png`
    link.href = objectUrl
    document.body.appendChild(link)
    link.click()
    setActionButtonSuccess(elements.downloadButton, '下载已开始')
    scheduleActionButtonRestore(elements.downloadButton, buttonSnapshot)
    showMessage('图片下载已开始！', 'success')
  } catch (error) {
    console.error('Unable to download the share image.', error)
    restoreActionButton(elements.downloadButton, buttonSnapshot)
    showMessage('下载失败，请右键点击图片另存为', 'danger')
  } finally {
    link?.remove()
    const urlToRevoke = objectUrl
    if (urlToRevoke) {
      window.setTimeout(() => URL.revokeObjectURL(urlToRevoke), 1000)
    }
  }
}

function findShareImageElements(root: HTMLElement): ShareImageElements | null {
  const generateButton = root.querySelector<HTMLButtonElement>('[data-generate-share-image]')
  const previewImage = root.querySelector<HTMLImageElement>('[data-share-preview]')
  const spinner = root.querySelector<HTMLElement>('[data-share-image-spinner]')
  const modal = document.querySelector<HTMLElement>('[data-share-image-modal]')
  const canvas = modal?.querySelector<HTMLCanvasElement>('[data-share-image-canvas]')
  const copyButton = modal?.querySelector<HTMLButtonElement>('[data-copy-share-image]')
  const downloadButton = modal?.querySelector<HTMLButtonElement>('[data-download-share-image]')

  if (
    !generateButton
    || !previewImage
    || !spinner
    || !modal
    || !canvas
    || !copyButton
    || !downloadButton
  ) {
    return null
  }
  return {
    canvas,
    copyButton,
    downloadButton,
    generateButton,
    modal,
    previewImage,
    root,
    spinner,
  }
}

function initializeShareImage(root: HTMLElement): void {
  const elements = findShareImageElements(root)
  if (!elements) {
    return
  }
  synchronizeGenerateAvailability(elements)
  root.addEventListener('share:content-revealed', () => {
    synchronizeGenerateAvailability(elements)
  })
  elements.modal.addEventListener('hidden.bs.modal', () => {
    elements.generateButton.focus({ preventScroll: true })
  })
  elements.generateButton.addEventListener('click', () => {
    void generateShareImage(elements)
  })
  elements.copyButton.addEventListener('click', () => {
    void copyShareImage(elements)
  })
  elements.downloadButton.addEventListener('click', () => {
    void downloadShareImage(elements)
  })
}

export function initializeShareImages(): void {
  document.querySelectorAll<HTMLElement>('[data-share-detail]').forEach(initializeShareImage)
}
