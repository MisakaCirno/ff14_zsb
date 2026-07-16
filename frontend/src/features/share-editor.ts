import { showMessage } from '../core/notify'

type QuillChangeSource = 'api' | 'silent' | 'user'

interface QuillEditor {
  clipboard: {
    dangerouslyPasteHTML(html: string): void
  }
  getSemanticHTML(): string
  getText(): string
  on(
    eventName: 'text-change',
    handler: (delta: unknown, oldDelta: unknown, source: QuillChangeSource) => void,
  ): void
  root: HTMLElement
}

interface QuillConstructor {
  new (container: Element, options: Record<string, unknown>): QuillEditor
}

declare global {
  interface Window {
    Quill?: QuillConstructor
  }
}

const toolbar = [
  ['bold', 'italic', 'underline', 'strike'],
  [{ header: [2, 3, false] }],
  [{ list: 'ordered' }, { list: 'bullet' }],
  ['blockquote', 'code-block', 'link'],
  [{ align: [] }],
  ['clean'],
]

const toolbarButtonLabels: Record<string, string> = {
  'ql-bold': '粗体',
  'ql-italic': '斜体',
  'ql-underline': '下划线',
  'ql-strike': '删除线',
  'ql-list:ordered': '编号列表',
  'ql-list:bullet': '项目列表',
  'ql-blockquote': '引用',
  'ql-code-block': '代码块',
  'ql-link': '链接',
  'ql-clean': '清除格式',
}

function labelToolbarControl(control: HTMLElement, label: string): void {
  control.setAttribute('aria-label', label)
  control.setAttribute('title', label)
}

function labelPicker(
  shell: HTMLElement,
  selector: string,
  label: string,
  itemLabels: Record<string, string>,
): void {
  const picker = shell.querySelector<HTMLElement>(selector)
  const pickerLabel = picker?.querySelector<HTMLElement>('.ql-picker-label')
  if (!picker || !pickerLabel) {
    return
  }

  labelToolbarControl(pickerLabel, label)
  picker.querySelectorAll<HTMLElement>('.ql-picker-item').forEach((item) => {
    const value = item.dataset.value || 'default'
    const itemLabel = itemLabels[value]
    if (itemLabel) {
      labelToolbarControl(item, itemLabel)
    }
  })
}

function configureToolbarAccessibility(shell: HTMLElement): void {
  const toolbarElement = shell.querySelector<HTMLElement>('.ql-toolbar')
  if (!toolbarElement) {
    return
  }

  toolbarElement.setAttribute('aria-label', '描述格式')
  toolbarElement.querySelectorAll<HTMLButtonElement>('button').forEach((button) => {
    const formatClass = Array.from(button.classList).find((name) => name.startsWith('ql-'))
    if (!formatClass) {
      return
    }
    const value = button.value
    const label = toolbarButtonLabels[value ? `${formatClass}:${value}` : formatClass]
    if (label) {
      labelToolbarControl(button, label)
    }
  })

  labelPicker(shell, '.ql-picker.ql-header', '段落样式', {
    default: '正文',
    '2': '二级标题',
    '3': '三级标题',
  })
  labelPicker(shell, '.ql-picker.ql-align', '文字对齐', {
    default: '左对齐',
    center: '居中对齐',
    right: '右对齐',
    justify: '两端对齐',
  })
}

function autoFixStrategyCode(input: HTMLTextAreaElement): void {
  const normalized = input.value.trim().replaceAll('【', '[').replaceAll('】', ']')
  const match = normalized.match(/\[stgy:[^\]\s]+\]/)
  if (!match || input.value === match[0]) {
    return
  }

  input.value = match[0]
}

function initializeStrategyCode(input: HTMLTextAreaElement): void {
  input.addEventListener('blur', () => autoFixStrategyCode(input))
  input.addEventListener('paste', () => {
    window.setTimeout(() => autoFixStrategyCode(input), 100)
  })
}

function uniqueElementId(preferredId: string): string {
  let candidate = preferredId
  let suffix = 2
  while (document.getElementById(candidate)) {
    candidate = `${preferredId}-${suffix}`
    suffix += 1
  }
  return candidate
}

function ensureElementId(element: HTMLElement, preferredId: string): string {
  if (element.id) {
    return element.id
  }
  element.id = uniqueElementId(preferredId)
  return element.id
}

function descriptionLabel(
  form: HTMLFormElement,
  input: HTMLTextAreaElement,
  shell: HTMLElement,
): HTMLElement {
  const configuredLabelId = shell.dataset.shareLabelId
  const configuredLabel = configuredLabelId
    ? document.getElementById(configuredLabelId)
    : null
  if (configuredLabel) {
    return configuredLabel
  }

  const label = Array.from(form.querySelectorAll<HTMLLabelElement>('label[for]'))
    .find((candidate) => candidate.htmlFor === input.id)
  if (label) {
    return label
  }

  const fallback = document.createElement('span')
  fallback.className = 'visually-hidden'
  fallback.textContent = input.getAttribute('aria-label') || '描述'
  shell.prepend(fallback)
  return fallback
}

function descriptionReferences(
  input: HTMLTextAreaElement,
  shell: HTMLElement,
): string {
  const references = new Set<string>()
  const addExistingReference = (id: string | undefined): void => {
    if (id && document.getElementById(id)) {
      references.add(id)
    }
  }

  addExistingReference(shell.dataset.shareHelpId)
  addExistingReference(shell.dataset.shareErrorId)
  ;(input.getAttribute('aria-describedby') || '')
    .split(/\s+/)
    .filter(Boolean)
    .forEach(addExistingReference)

  const fieldGroup = input.closest<HTMLElement>('.mb-3, [data-share-field]')
  fieldGroup?.querySelectorAll<HTMLElement>(
    '.form-text, .invalid-feedback, .text-danger, [data-share-description-help]',
  ).forEach((element, index) => {
    references.add(ensureElementId(element, `${input.id}-description-${index + 1}`))
  })

  if (references.size === 0) {
    const guidance = document.createElement('span')
    guidance.className = 'visually-hidden'
    guidance.textContent = '可选的富文本描述编辑器。'
    shell.append(guidance)
    references.add(ensureElementId(guidance, `${input.id}-description`))
  }
  return Array.from(references).join(' ')
}

function configureEditorAccessibility(
  form: HTMLFormElement,
  input: HTMLTextAreaElement,
  shell: HTMLElement,
  quill: QuillEditor,
): void {
  const inputId = input.id || ensureElementId(input, 'share-description')
  const label = descriptionLabel(form, input, shell)
  quill.root.setAttribute('role', 'textbox')
  quill.root.setAttribute('aria-multiline', 'true')
  quill.root.tabIndex = 0
  quill.root.setAttribute(
    'aria-labelledby',
    ensureElementId(label, `${inputId}-label`),
  )
  quill.root.setAttribute('aria-describedby', descriptionReferences(input, shell))
  quill.root.setAttribute(
    'aria-invalid',
    input.getAttribute('aria-invalid') === 'true' ? 'true' : 'false',
  )
}

function showNativeDescription(
  source: HTMLElement | null,
  shell: HTMLElement | null,
  editorContainer: HTMLElement | null,
): void {
  if (source) {
    source.hidden = false
    source.classList.remove('d-none')
  }
  if (shell) {
    shell.hidden = true
  } else if (editorContainer) {
    editorContainer.hidden = true
  }
}

function showEnhancedDescription(source: HTMLElement, shell: HTMLElement): void {
  source.hidden = true
  shell.hidden = false
}

function initializeSubmission(
  form: HTMLFormElement,
  serializeDescription?: () => boolean,
): void {
  let submitting = false
  const submitControls = Array.from(
    form.querySelectorAll<HTMLButtonElement | HTMLInputElement>(
      'button[type="submit"], input[type="submit"]',
    ),
  )

  form.addEventListener('submit', (event) => {
    if (submitting) {
      event.preventDefault()
      return
    }
    if (serializeDescription && !serializeDescription()) {
      event.preventDefault()
      return
    }

    submitting = true
    form.setAttribute('aria-busy', 'true')
    const enabledControls = submitControls.filter((control) => !control.disabled)
    enabledControls.forEach((control) => control.setAttribute('aria-disabled', 'true'))

    queueMicrotask(() => {
      if (event.defaultPrevented) {
        submitting = false
        form.removeAttribute('aria-busy')
        enabledControls.forEach((control) => control.removeAttribute('aria-disabled'))
        return
      }
      enabledControls.forEach((control) => {
        control.disabled = true
      })
    })
  })
}

function initializeEditor(form: HTMLFormElement): void {
  if (form.dataset.shareEditorInitialized === 'true') {
    return
  }
  form.dataset.shareEditorInitialized = 'true'
  form.querySelector<HTMLElement>('[data-form-error-summary]')
    ?.focus()

  const strategyCodeInput = form.querySelector<HTMLTextAreaElement>(
    '[data-share-strategy-code]',
  )
  if (strategyCodeInput) {
    initializeStrategyCode(strategyCodeInput)
  }

  const descriptionInput = form.querySelector<HTMLTextAreaElement>(
    '[data-share-description]',
  )
  const descriptionSource = form.querySelector<HTMLElement>('[data-share-description-source]')
  const editorShell = form.querySelector<HTMLElement>('[data-share-rich-text-shell]')
  const editorContainer = form.querySelector<HTMLElement>('[data-share-rich-text-editor]')
  showNativeDescription(descriptionSource, editorShell, editorContainer)

  const Quill = window.Quill
  if (
    !descriptionInput
    || !descriptionSource
    || !editorShell
    || !editorContainer
    || typeof Quill !== 'function'
  ) {
    initializeSubmission(form)
    return
  }

  let quill: QuillEditor
  try {
    quill = new Quill(editorContainer, {
      theme: 'snow',
      placeholder: '请输入描述内容...',
      modules: { toolbar },
    })
    if (descriptionInput.value) {
      quill.clipboard.dangerouslyPasteHTML(descriptionInput.value)
    }
    configureToolbarAccessibility(editorShell)
    configureEditorAccessibility(form, descriptionInput, editorShell, quill)
  } catch (error) {
    console.warn('Unable to initialize the rich text editor.', error)
    showNativeDescription(descriptionSource, editorShell, editorContainer)
    initializeSubmission(form)
    return
  }

  let descriptionDirty = false
  let editorActive = true
  quill.on('text-change', (_delta, _oldDelta, source) => {
    if (source === 'user') {
      descriptionDirty = true
    }
  })
  showEnhancedDescription(descriptionSource, editorShell)

  initializeSubmission(form, () => {
    if (!editorActive || !descriptionDirty) {
      return true
    }
    try {
      descriptionInput.value = quill.getText().trim().length === 0
        ? ''
        : quill.getSemanticHTML()
      return true
    } catch (error) {
      console.warn('Unable to serialize the rich text editor.', error)
      descriptionInput.value = quill.root.innerHTML
      editorActive = false
      descriptionDirty = false
      showNativeDescription(descriptionSource, editorShell, editorContainer)
      showMessage('富文本编辑器暂时不可用，已切换到文本框，请确认内容后重新提交。', 'warning')
      descriptionInput.focus()
      return false
    }
  })
}

export function initializeShareEditors(): void {
  document.querySelectorAll<HTMLFormElement>('[data-share-editor]').forEach(initializeEditor)
}
