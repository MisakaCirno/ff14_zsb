import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import models
from django.template.loader import get_template
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .admin_forms import AnnouncementAdminForm, ShareAdminForm
from .forms import ShareForm
from .models import Announcement
from .widgets import QuillWidget


class QuillEditorIntegrationTests(SimpleTestCase):
    def test_ckeditor_is_not_loaded_at_runtime(self):
        self.assertNotIn('ckeditor', settings.INSTALLED_APPS)
        self.assertIs(type(Announcement._meta.get_field('content')), models.TextField)

    def test_fresh_migration_replay_does_not_require_ckeditor_package(self):
        requirements = (
            Path(settings.BASE_DIR) / 'requirements.txt'
        ).read_text(encoding='utf-8')
        historical_migration = (
            Path(settings.BASE_DIR)
            / 'shares'
            / 'migrations'
            / '0011_alter_announcement_content.py'
        ).read_text(encoding='utf-8')

        self.assertNotIn('django-ckeditor', requirements)
        self.assertNotIn('ckeditor.fields', historical_migration)
        self.assertIn('models.TextField', historical_migration)

    def test_admin_rich_text_fields_use_quill_widget(self):
        self.assertIsInstance(AnnouncementAdminForm.base_fields['content'].widget, QuillWidget)
        self.assertIsInstance(ShareAdminForm.base_fields['description'].widget, QuillWidget)

        media = str(AnnouncementAdminForm().media)
        self.assertIn('css/quill.snow.css', media)
        self.assertIn('js/quill.js', media)
        self.assertIn('js/quill-widget.js', media)

    def test_admin_uses_framework_shell_and_editor_keeps_responsive_fixes(self):
        project_admin_template = (
            Path(settings.BASE_DIR) / 'templates' / 'admin' / 'base_site.html'
        )
        project_admin_styles = (
            Path(settings.BASE_DIR) / 'static' / 'css' / 'admin-shell.css'
        )
        editor_script = (
            Path(settings.BASE_DIR) / 'static' / 'js' / 'quill-widget.js'
        ).read_text(encoding='utf-8')
        editor_styles = (
            Path(settings.BASE_DIR) / 'static' / 'css' / 'quill-widget.css'
        ).read_text(encoding='utf-8')

        self.assertFalse(project_admin_template.exists())
        self.assertFalse(project_admin_styles.exists())
        self.assertIn("wrapper.className = 'quill-admin-field'", editor_script)
        self.assertIn('wrapper.appendChild(editor)', editor_script)
        self.assertIn("source.insertAdjacentElement('afterend', wrapper)", editor_script)
        self.assertLess(
            editor_script.index('wrapper.appendChild(editor)'),
            editor_script.index('new window.Quill'),
        )
        self.assertIn("toolbar.setAttribute('role', 'toolbar')", editor_script)
        self.assertIn("quill.root.setAttribute('role', 'textbox')", editor_script)
        self.assertIn('let contentDirty = false', editor_script)
        self.assertIn("if (origin === 'user')", editor_script)
        self.assertIn('if (contentDirty)', editor_script)
        self.assertIn('.quill-admin-field {', editor_styles)
        self.assertIn('flex: 1 1 48rem;', editor_styles)
        self.assertIn('width: min(100%, 72rem);', editor_styles)
        self.assertIn('height: auto;', editor_styles)
        self.assertIn('@media (max-width: 767px)', editor_styles)

    def test_public_editors_share_one_native_first_form_partial(self):
        partial_name = 'shares/includes/share_editor_form.html'
        partial_source = get_template(partial_name).template.source

        for template_name in ('shares/create.html', 'shares/edit.html'):
            with self.subTest(template=template_name):
                source = get_template(template_name).template.source
                self.assertIn(partial_name, source)
                self.assertIn("static 'js/quill.js'", source)
                self.assertNotIn('<script>', source)
                self.assertNotIn('<style>', source)

        self.assertIn('data-share-editor', partial_source)
        self.assertIn('data-share-description-source', partial_source)
        self.assertIn('data-share-rich-text-shell', partial_source)
        self.assertIn('data-share-rich-text-editor', partial_source)
        self.assertIn('data-form-error-summary', partial_source)
        self.assertIn('<fieldset', partial_source)
        self.assertIn('<legend', partial_source)
        self.assertNotIn('data-validate-strategy-code', partial_source)
        self.assertNotRegex(
            partial_source,
            re.compile(
                r'<(?=[^>]*data-share-description-source)'
                r'(?=[^>]*class=["\'][^"\']*\bd-none\b)[^>]+>',
                re.IGNORECASE,
            ),
        )
        self.assertRegex(
            partial_source,
            re.compile(
                r'<(?=[^>]*data-share-rich-text-shell)'
                r'(?=[^>]*\shidden(?:\s|=|>))[^>]+>',
                re.IGNORECASE,
            ),
        )

    def test_public_editors_enhance_quill_without_rewriting_untouched_html(self):
        module_source = (
            Path(settings.BASE_DIR) / 'frontend' / 'src' / 'features' / 'share-editor.ts'
        ).read_text(encoding='utf-8')

        self.assertIn('quill.clipboard.dangerouslyPasteHTML', module_source)
        self.assertIn('quill.getSemanticHTML()', module_source)
        self.assertIn('let descriptionDirty = false', module_source)
        self.assertIn("quill.on('text-change'", module_source)
        self.assertRegex(module_source, r"source\s*===\s*['\"]user['\"]")
        self.assertRegex(
            module_source,
            re.compile(
                r'if\s*\([^)]*!descriptionDirty[^)]*\)\s*\{\s*return true',
                re.DOTALL,
            ),
        )
        self.assertIn('try {', module_source)
        self.assertIn('catch', module_source)
        self.assertIn('source.hidden = false', module_source)
        self.assertIn('shell.hidden = true', module_source)
        self.assertIn('source.hidden = true', module_source)
        self.assertIn('shell.hidden = false', module_source)
        self.assertLess(
            module_source.index('new Quill'),
            module_source.rindex('showEnhancedDescription(descriptionSource, editorShell)'),
        )

    def test_enhanced_editor_exposes_accessibility_and_busy_state(self):
        module_source = (
            Path(settings.BASE_DIR) / 'frontend' / 'src' / 'features' / 'share-editor.ts'
        ).read_text(encoding='utf-8')

        for attribute in (
            'role',
            'aria-multiline',
            'aria-labelledby',
            'aria-describedby',
            'aria-invalid',
        ):
            with self.subTest(attribute=attribute):
                self.assertIn(attribute, module_source)

        self.assertIn('quill.root.tabIndex = 0', module_source)
        self.assertIn("toolbarElement.setAttribute('aria-label', '描述格式')", module_source)
        self.assertIn("labelPicker(shell, '.ql-picker.ql-header', '段落样式'", module_source)
        self.assertIn("labelPicker(shell, '.ql-picker.ql-align', '文字对齐'", module_source)
        self.assertIn("form.setAttribute('aria-busy', 'true')", module_source)
        self.assertRegex(module_source, r'\.disabled\s*=\s*true')

    def test_public_editor_form_exposes_frontend_hooks(self):
        rendered_form = ShareForm().as_p()

        self.assertIn('data-share-strategy-code="true"', rendered_form)
        self.assertIn('data-share-description="true"', rendered_form)

    def test_vendored_quill_is_version_two(self):
        license_notice = (
            Path(settings.BASE_DIR) / 'static' / 'js' / 'quill.js.LICENSE.txt'
        ).read_text(encoding='utf-8')

        self.assertIn('Quill Editor v2.0.3', license_notice)


class AdminEditorPageIntegrationTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            username='editor-admin',
            email='editor-admin@example.test',
            password='password123',
        )
        self.client.force_login(self.admin)

    def test_framework_admin_shell_loads_only_editor_customizations(self):
        index_response = self.client.get(reverse('admin:index'))
        editor_response = self.client.get(reverse('admin:shares_announcement_add'))

        self.assertEqual(index_response.status_code, 200)
        self.assertEqual(editor_response.status_code, 200)
        self.assertNotContains(index_response, 'css/admin-shell.css')
        self.assertNotContains(index_response, 'admin-brand__link')
        self.assertNotContains(editor_response, 'css/admin-shell.css')
        self.assertContains(editor_response, 'css/quill-widget.css')
        self.assertContains(editor_response, 'js/quill-widget.js')
        self.assertContains(editor_response, 'js/quill.js')
        self.assertContains(editor_response, 'quill-editor-source')
