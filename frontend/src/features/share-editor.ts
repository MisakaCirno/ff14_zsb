import { showMessage } from '../core/notify'

interface QuillEditor {
  clipboard: {
    dangerouslyPasteHTML(html: string): void
  }
  getSemanticHTML(): string
  getText(): string
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
  ['blockquote', 'code-block'],
  [{ header: 1 }, { header: 2 }],
  [{ list: 'ordered' }, { list: 'bullet' }],
  [{ script: 'sub' }, { script: 'super' }],
  [{ indent: '-1' }, { indent: '+1' }],
  [{ direction: 'rtl' }],
  [{ size: ['small', false, 'large', 'huge'] }],
  [{ header: [1, 2, 3, 4, 5, 6, false] }],
  [{ color: [] }, { background: [] }],
  [{ font: [] }],
  [{ align: [] }],
  ['clean'],
]

function autoFixStrategyCode(input: HTMLTextAreaElement): void {
  const value = input.value.trim()
  if (!value) {
    return
  }

  let match = value.match(/\[stgy:[^\]]+\]/)
  if (!match && value.toLowerCase().includes('stgy:')) {
    match = value.replaceAll('【', '[').replaceAll('】', ']').match(/\[stgy:[^\]]+\]/)
  }
  if (!match || input.value === match[0]) {
    return
  }

  input.value = match[0]
  input.classList.add('is-valid')
  window.setTimeout(() => input.classList.remove('is-valid'), 1000)
}

function initializeStrategyCode(input: HTMLTextAreaElement): void {
  input.addEventListener('blur', () => autoFixStrategyCode(input))
  input.addEventListener('paste', () => {
    window.setTimeout(() => autoFixStrategyCode(input), 100)
  })
}

function initializeEditor(form: HTMLFormElement): void {
  const strategyCodeInput = form.querySelector<HTMLTextAreaElement>(
    '[data-share-strategy-code]',
  )
  const descriptionInput = form.querySelector<HTMLTextAreaElement>(
    '[data-share-description]',
  )
  const descriptionSource = form.querySelector<HTMLElement>('[data-share-description-source]')
  const editorContainer = form.querySelector<HTMLElement>('[data-share-rich-text-editor]')
  const Quill = window.Quill

  if (!strategyCodeInput || !descriptionInput || !descriptionSource || !editorContainer) {
    return
  }

  initializeStrategyCode(strategyCodeInput)
  if (typeof Quill !== 'function') {
    descriptionSource.classList.remove('d-none')
    editorContainer.hidden = true
    return
  }

  const quill = new Quill(editorContainer, {
    theme: 'snow',
    placeholder: '请输入描述内容...',
    modules: { toolbar },
  })

  if (descriptionInput.value) {
    quill.clipboard.dangerouslyPasteHTML(descriptionInput.value)
  }

  form.addEventListener('submit', (event) => {
    if (form.hasAttribute('data-validate-strategy-code')) {
      const code = strategyCodeInput.value.trim()
      if (!code.startsWith('[stgy:') || !code.endsWith(']')) {
        showMessage(
          '战术板代码格式不正确！\n必须以 "[stgy:" 开头，并以 "]" 结尾。',
          'danger',
        )
        event.preventDefault()
        strategyCodeInput.focus()
        return
      }
    }

    descriptionInput.value = quill.getText().trim().length === 0
      ? ''
      : quill.getSemanticHTML()
  })
}

export function initializeShareEditors(): void {
  document.querySelectorAll<HTMLFormElement>('[data-share-editor]').forEach(initializeEditor)
}
