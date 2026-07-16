// @vitest-environment jsdom

import { beforeEach, describe, expect, it } from 'vitest'

import { initializeShareEditors } from './share-editor'

describe('share editor server errors', () => {
  beforeEach(() => {
    delete window.Quill
    document.body.innerHTML = `
      <form data-share-editor>
        <div data-form-error-summary tabindex="-1">提交内容需要检查</div>
        <textarea data-share-strategy-code></textarea>
      </form>
    `
  })

  it('focuses the server-rendered error summary without requiring a rich text editor', () => {
    const summary = document.querySelector<HTMLElement>('[data-form-error-summary]')!

    initializeShareEditors()

    expect(document.activeElement).toBe(summary)
  })

  it('does not steal focus again after the form has already been initialized', () => {
    const outside = document.createElement('button')
    document.body.append(outside)
    initializeShareEditors()
    outside.focus()

    initializeShareEditors()

    expect(document.activeElement).toBe(outside)
  })
})
