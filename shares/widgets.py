from django import forms


class QuillWidget(forms.Textarea):
    """Progressively enhance a textarea with the vendored Quill editor."""

    class Media:
        css = {
            'all': (
                'css/quill.snow.css',
                'css/quill-widget.css',
            ),
        }
        js = (
            'js/quill.js',
            'js/quill-widget.js',
        )

    def __init__(self, attrs=None):
        default_attrs = {
            'class': 'vLargeTextField quill-editor-source',
            'data-quill-placeholder': '请输入内容…',
            'rows': 12,
        }
        if attrs:
            default_attrs.update(attrs)
        super().__init__(default_attrs)
