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

    function initialize(source) {
        if (source.dataset.quillInitialized === 'true' || typeof window.Quill !== 'function') {
            return;
        }

        const editor = document.createElement('div');
        editor.className = 'quill-admin-editor';
        source.insertAdjacentElement('afterend', editor);

        const quill = new window.Quill(editor, {
            modules: {toolbar: TOOLBAR},
            placeholder: source.dataset.quillPlaceholder || '请输入内容…',
            theme: 'snow',
        });

        if (source.value) {
            quill.clipboard.dangerouslyPasteHTML(source.value);
        }

        source.hidden = true;
        source.dataset.quillInitialized = 'true';

        const form = source.closest('form');
        if (form) {
            form.addEventListener('submit', function () {
                synchronize(source, quill);
            });
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
