from pathlib import Path

from django.conf import settings
from django.db import models
from django.template.loader import get_template
from django.test import SimpleTestCase

from .admin_forms import AnnouncementAdminForm, ShareAdminForm
from .forms import ShareForm
from .models import Announcement
from .widgets import QuillWidget


class QuillEditorIntegrationTests(SimpleTestCase):
    def test_ckeditor_is_not_loaded_at_runtime(self):
        self.assertNotIn('ckeditor', settings.INSTALLED_APPS)
        self.assertIs(type(Announcement._meta.get_field('content')), models.TextField)

    def test_admin_rich_text_fields_use_quill_widget(self):
        self.assertIsInstance(AnnouncementAdminForm.base_fields['content'].widget, QuillWidget)
        self.assertIsInstance(ShareAdminForm.base_fields['description'].widget, QuillWidget)

        media = str(AnnouncementAdminForm().media)
        self.assertIn('css/quill.snow.css', media)
        self.assertIn('js/quill.js', media)
        self.assertIn('js/quill-widget.js', media)

    def test_public_editors_use_quill_2_html_api(self):
        module_source = (
            Path(settings.BASE_DIR) / 'frontend' / 'src' / 'features' / 'share-editor.ts'
        ).read_text(encoding='utf-8')

        self.assertIn('quill.clipboard.dangerouslyPasteHTML', module_source)
        self.assertIn('quill.getSemanticHTML()', module_source)
        template_sources = {}
        for template_name in ('shares/create.html', 'shares/edit.html'):
            with self.subTest(template=template_name):
                source = get_template(template_name).template.source
                template_sources[template_name] = source
                self.assertIn('data-share-editor', source)
                self.assertIn('data-share-description-source', source)
                self.assertIn('data-share-rich-text-editor', source)
                self.assertIn("static 'js/quill.js'", source)
                self.assertNotIn('<script>', source)
                self.assertNotIn('<style>', source)
                self.assertNotIn('quill.root.innerHTML = descriptionInput.value', source)

        self.assertIn(
            'data-validate-strategy-code',
            template_sources['shares/create.html'],
        )
        self.assertNotIn(
            'data-validate-strategy-code',
            template_sources['shares/edit.html'],
        )

    def test_public_editor_form_exposes_frontend_hooks(self):
        rendered_form = ShareForm().as_p()

        self.assertIn('data-share-strategy-code="true"', rendered_form)
        self.assertIn('data-share-description="true"', rendered_form)

    def test_vendored_quill_is_version_two(self):
        license_notice = (
            Path(settings.BASE_DIR) / 'static' / 'js' / 'quill.js.LICENSE.txt'
        ).read_text(encoding='utf-8')

        self.assertIn('Quill Editor v2.0.3', license_notice)
