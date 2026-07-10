from pathlib import Path

from django.conf import settings
from django.db import models
from django.template.loader import get_template
from django.test import SimpleTestCase

from .admin_forms import AnnouncementAdminForm, ShareAdminForm
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
        for template_name in ('shares/create.html', 'shares/edit.html'):
            with self.subTest(template=template_name):
                source = get_template(template_name).template.source
                self.assertIn('quill.clipboard.dangerouslyPasteHTML', source)
                self.assertIn('quill.getSemanticHTML()', source)
                self.assertNotIn('quill.root.innerHTML = descriptionInput.value', source)

    def test_vendored_quill_is_version_two(self):
        license_notice = (
            Path(settings.BASE_DIR) / 'static' / 'js' / 'quill.js.LICENSE.txt'
        ).read_text(encoding='utf-8')

        self.assertIn('Quill Editor v2.0.3', license_notice)
