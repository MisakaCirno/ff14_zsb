(function () {
    'use strict';

    const TOOLBAR = [
        ['bold', 'italic', 'underline', 'strike'],
        ['blockquote', 'code-block', 'link'],
        [{header: [1, 2, 3, 4, 5, 6, false]}],
        [{list: 'ordered'}, {list: 'bullet'}],
        [{script: 'sub'}, {script: 'super'}],
        [{indent: '-1'}, {indent: '+1'}],
        [{direction: 'rtl'}, {align: []}],
        [{size: ['small', false, 'large', 'huge']}],
        [{color: []}, {background: []}],
        [{font: []}],
        ['clean'],
    ];

    function synchronize(source, quill) {
        source.value = quill.getText().trim().length === 0
            ? ''
            : quill.getSemanticHTML();
    }

    function connectAccessibility(source, quill, wrapper) {
        const label = source.id
            ? document.querySelector(`label[for="${CSS.escape(source.id)}"]`)
            : null;
        if (label && !label.id) {
            label.id = `${source.id}-label`;
        }

        quill.root.tabIndex = 0;
        quill.root.setAttribute('role', 'textbox');
        quill.root.setAttribute('aria-multiline', 'true');
        if (label && label.id) {
            quill.root.setAttribute('aria-labelledby', label.id);
        }

        for (const attribute of ['aria-describedby', 'aria-invalid']) {
            const value = source.getAttribute(attribute);
            if (value) {
                quill.root.setAttribute(attribute, value);
            }
        }

        const toolbar = wrapper.querySelector('.ql-toolbar');
        if (toolbar) {
            toolbar.setAttribute('role', 'toolbar');
            toolbar.setAttribute('aria-label', '内容格式');
        }
    }

    function initialize(source) {
        if (source.dataset.quillInitialized === 'true' || typeof window.Quill !== 'function') {
            return;
        }

        const wrapper = document.createElement('div');
        wrapper.className = 'quill-admin-field';
        wrapper.hidden = true;

        const editor = document.createElement('div');
        editor.className = 'quill-admin-editor';
        wrapper.appendChild(editor);
        source.insertAdjacentElement('afterend', wrapper);

        try {
            const quill = new window.Quill(editor, {
                modules: {toolbar: TOOLBAR},
                placeholder: source.dataset.quillPlaceholder || '请输入内容…',
                theme: 'snow',
            });

            if (source.value) {
                quill.clipboard.dangerouslyPasteHTML(source.value);
            }

            connectAccessibility(source, quill, wrapper);

            let contentDirty = false;
            quill.on('text-change', function (_delta, _oldDelta, origin) {
                if (origin === 'user') {
                    contentDirty = true;
                }
            });

            const form = source.closest('form');
            if (form) {
                form.addEventListener('submit', function () {
                    if (contentDirty) {
                        synchronize(source, quill);
                    }
                });
            }

            source.hidden = true;
            source.dataset.quillInitialized = 'true';
            wrapper.hidden = false;
        } catch (error) {
            wrapper.remove();
            source.hidden = false;
            console.error('Unable to initialize the admin rich text editor.', error);
        }
    }

    function initializeAll(root) {
        root.querySelectorAll('textarea.quill-editor-source').forEach(initialize);
    }

    function start() {
        initializeAll(document);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }

    document.addEventListener('formset:added', function (event) {
        initializeAll(event.target);
    });
}());
