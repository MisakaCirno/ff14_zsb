import re
from pathlib import Path

from django.conf import settings
from django.contrib.messages import constants as message_constants
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .models import Share


class FrontendShellContractTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(username='author', password='password123')
        self.share = Share.objects.create(
            title='前端契约 & "测试"',
            strategy_code='[stgy:a&"b]',
            author=self.author,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )
        self.author.profile.nickname = '作者 & "昵称"'
        self.author.profile.save(update_fields=['nickname'])

    def test_shell_loads_vite_without_legacy_vue_or_inline_base_logic(self):
        response = self.client.get(reverse('index'))
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertRegex(content, r'<meta name="csrf-token" content="[^"]+">')
        self.assertIn('csrftoken', response.cookies)
        self.assertNotIn('vue.global.js', content)
        self.assertNotIn('function updateHistoryDropdown', content)

    def test_error_feedback_uses_bootstrap_and_accessibility_contracts(self):
        response = self.client.post(
            reverse('set_home_feed_mode'),
            {'feed': 'invalid'},
            follow=True,
        )
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn('class="app-notifications ', content)
        self.assertIn('role="region"', content)
        self.assertIn('aria-live="polite"', content)
        self.assertIn('alert-danger', content)
        self.assertNotIn('alert-error', content)
        self.assertIn('aria-label="关闭通知"', content)
        self.assertEqual(settings.MESSAGE_TAGS[message_constants.DEBUG], 'secondary')
        self.assertEqual(settings.MESSAGE_TAGS[message_constants.ERROR], 'danger')

    def test_share_copy_button_uses_escaped_data_contract(self):
        response = self.client.get(reverse('index'))
        content = response.content.decode()

        self.assertIn('data-copy-strategy', content)
        self.assertIn('data-copy-code="[stgy:a&amp;&quot;b]"', content)
        self.assertIn(f'data-share-id="{self.share.share_id}"', content)
        self.assertNotIn('copyStrategyCode(', content)

    def test_authenticated_card_reactions_use_htmx_fragments(self):
        self.client.force_login(self.author)

        response = self.client.get(reverse('index'))
        content = response.content.decode()

        self.assertIn(
            f'hx-post="/share/{self.share.share_id}/like/?fragment=card"',
            content,
        )
        self.assertIn(
            f'hx-post="/share/{self.share.share_id}/favorite/?fragment=card"',
            content,
        )
        self.assertNotIn('toggleIndexLike(', content)
        self.assertNotIn('toggleIndexFavorite(', content)

    def test_authenticated_detail_reactions_use_htmx_fragments(self):
        self.client.force_login(self.author)

        response = self.client.get(reverse('share_detail', args=[self.share.share_id]))
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn('data-share-detail', content)
        self.assertIn(f'data-share-id="{self.share.share_id}"', content)
        self.assertIn('data-share-title="前端契约 &amp; &quot;测试&quot;"', content)
        self.assertIn('data-share-author="作者 &amp; &quot;昵称&quot;"', content)
        self.assertIn(
            f'data-record-view-url="/share/{self.share.share_id}/view/"',
            content,
        )
        self.assertIn(
            f'data-record-copy-url="/share/{self.share.share_id}/copy/"',
            content,
        )
        self.assertIn(
            f'hx-post="/share/{self.share.share_id}/like/?fragment=detail"',
            content,
        )
        self.assertIn(
            f'hx-post="/share/{self.share.share_id}/favorite/?fragment=detail"',
            content,
        )
        self.assertNotIn('toggleLike()', content)
        self.assertNotIn('toggleFavorite()', content)
        self.assertNotIn('function toggleLike', content)
        self.assertNotIn('function toggleFavorite', content)


class FrontendTemplateSourceTests(SimpleTestCase):
    def read_template(self, relative_path):
        return (Path(settings.BASE_DIR) / 'templates' / relative_path).read_text(
            encoding='utf-8',
        )

    def read_frontend(self, relative_path):
        return (Path(settings.BASE_DIR) / 'frontend' / 'src' / relative_path).read_text(
            encoding='utf-8',
        )

    def test_shared_copy_buttons_do_not_use_inline_handlers(self):
        template_paths = (
            'shares/includes/share_cards.html',
            'shares/my_shares.html',
            'shares/user_public_profile.html',
        )

        for template_path in template_paths:
            with self.subTest(template=template_path):
                source = self.read_template(template_path)
                self.assertIn('data-copy-strategy', source)
                self.assertNotIn('copyStrategyCode(', source)

    def test_base_template_has_no_classic_business_script(self):
        source = self.read_template('base.html')
        main_source = self.read_frontend('main.ts')

        self.assertNotIn('vue.global.js', source)
        self.assertNotIn('function showMessage', source)
        self.assertNotIn('function updateHistoryDropdown', source)
        self.assertNotIn('onclick="copyQQGroup', source)
        self.assertNotIn('onclick="clearHistory', source)
        self.assertNotIn('window.showMessage', main_source)
        self.assertNotIn('window.fallbackCopyTextToClipboard', main_source)
        self.assertNotIn('window.updateHistoryDropdown', main_source)

    def test_main_templates_do_not_use_inline_event_handlers(self):
        templates_root = Path(settings.BASE_DIR) / 'templates'

        for template_path in templates_root.rglob('*.html'):
            with self.subTest(template=template_path.relative_to(templates_root)):
                source = template_path.read_text(encoding='utf-8')
                self.assertNotRegex(
                    source,
                    re.compile(r'\son[a-z]+\s*=', re.IGNORECASE),
                )

    def test_application_styles_are_owned_by_vite_entry(self):
        templates_root = Path(settings.BASE_DIR) / 'templates'

        for template_path in templates_root.rglob('*.html'):
            with self.subTest(template=template_path.relative_to(templates_root)):
                source = template_path.read_text(encoding='utf-8')
                self.assertNotRegex(source, re.compile(r'<style\b', re.IGNORECASE))

        main_source = self.read_frontend('styles/main.css')
        style_imports = (
            "@import './tokens.css';",
            "@import './foundations.css';",
            "@import './components.css';",
            "@import './feedback.css';",
        )
        import_positions = []
        for style_import in style_imports:
            self.assertIn(style_import, main_source)
            import_positions.append(main_source.index(style_import))
        self.assertEqual(import_positions, sorted(import_positions))

        tokens_source = self.read_frontend('styles/tokens.css')
        self.assertIn('--app-color-primary:', tokens_source)
        self.assertIn('--app-font-sans:', tokens_source)
        self.assertIn('--app-space-4:', tokens_source)
        self.assertIn('--app-radius-lg:', tokens_source)
        self.assertIn('--app-shadow-hover:', tokens_source)
        self.assertIn('--app-focus-ring-color:', tokens_source)
        self.assertIn('--app-motion-normal:', tokens_source)

        components_source = self.read_frontend('styles/components.css')
        self.assertIn('.registration-form #id_username', components_source)
        self.assertIn('.admin-tabs .nav-link', components_source)
        self.assertIn(
            'class="registration-form"',
            self.read_template('shares/register.html'),
        )
        self.assertIn(
            'admin-tabs',
            self.read_template('shares/includes/admin_tabs.html'),
        )

        base_source = self.read_template('base.html')
        feedback_source = self.read_template('shares/includes/flash_messages.html')
        notify_source = self.read_frontend('core/notify.ts')
        self.assertIn("{% include 'shares/includes/flash_messages.html' %}", base_source)
        self.assertIn('app-notifications', feedback_source)
        self.assertIn('app-notification__message', feedback_source)
        self.assertIn('app-notification__message', notify_source)
        self.assertNotIn('messageText.style', notify_source)

    def test_common_template_events_use_data_contracts(self):
        for template_path in (
            'shares/includes/share_cards.html',
            'shares/my_shares.html',
            'shares/user_public_profile.html',
            'shares/collection_detail.html',
            'shares/admin_review_list.html',
        ):
            with self.subTest(template=template_path):
                source = self.read_template(template_path)
                self.assertIn('data-preview-frame', source)
                self.assertIn('data-preview-image', source)
                self.assertIn('data-preview-loading', source)

        self.assertIn(
            'data-submit-on-change',
            self.read_template('shares/index.html'),
        )
        self.assertIn(
            'data-confirm-message',
            self.read_template('shares/collection_detail.html'),
        )
        preview_source = self.read_frontend('features/preview-images.ts')
        controls_source = self.read_frontend('features/form-controls.ts')
        self.assertIn("image.addEventListener('load'", preview_source)
        self.assertIn("image.addEventListener('error'", preview_source)
        self.assertIn("document.addEventListener('htmx:load'", preview_source)
        self.assertIn('form?.requestSubmit()', controls_source)
        self.assertIn('window.confirm(message)', controls_source)

    def test_card_reaction_templates_do_not_use_inline_handlers(self):
        for template_path in (
            'shares/includes/share_cards.html',
            'shares/my_shares.html',
        ):
            with self.subTest(template=template_path):
                source = self.read_template(template_path)
                self.assertNotIn('toggleIndexLike(', source)
                self.assertNotIn('toggleIndexFavorite(', source)

    def test_home_template_delegates_announcement_and_infinite_scroll(self):
        source = self.read_template('shares/index.html')
        page_source = self.read_template('shares/includes/share_cards_page.html')

        self.assertIn('data-dismiss-announcement', source)
        self.assertIn("shares/includes/share_cards_page.html", source)
        self.assertNotIn('<script', source)
        self.assertNotIn('dismissAnnouncement()', source)
        self.assertNotIn('initInfiniteScroll', source)
        self.assertNotIn('insertAdjacentHTML', source)
        self.assertIn('data-infinite-scroll-sentinel', page_source)
        self.assertIn('hx-trigger="intersect, click"', page_source)
        self.assertNotIn('hx-trigger="revealed"', page_source)

    def test_detail_basic_interactions_use_module_contract(self):
        source = self.read_template('shares/detail.html')
        module_source = self.read_frontend('features/share-detail.ts')

        for hook in (
            'data-share-detail',
            'data-content-overlay',
            'data-copy-detail-code',
            'data-copy-share-url',
            'data-views-count',
            'data-copies-count',
        ):
            self.assertIn(hook, source)

        for legacy_handler in (
            'onclick="revealContent',
            'onclick="copyCode',
            'onclick="copyUrl',
            'function revealContent',
            'function copyCode',
            'function copyUrl',
        ):
            self.assertNotIn(legacy_handler, source)

        self.assertIn('recordVisitHistory(shareId, shareTitle)', module_source)
        self.assertIn("updateCounter(root, '[data-views-count]'", module_source)
        self.assertIn("updateCounter(root, '[data-copies-count]'", module_source)

    def test_share_image_uses_module_contract_and_only_qrcode_runtime(self):
        source = self.read_template('shares/detail.html')
        about_source = self.read_template('about.html')
        module_source = self.read_frontend('features/share-image.ts')

        for hook in (
            'data-share-author',
            'data-generate-share-image',
            'data-share-image-spinner',
            'data-share-image-modal',
            'data-share-image-canvas',
            'data-copy-share-image',
            'data-download-share-image',
        ):
            self.assertIn(hook, source)

        for legacy_handler in (
            'onclick="generateShareImage',
            'onclick="copyShareImage',
            'onclick="downloadShareImage',
            'function generateShareImage',
            'function copyShareImage',
            'function downloadShareImage',
        ):
            self.assertNotIn(legacy_handler, source)

        self.assertNotIn('<script>', source)
        self.assertNotIn('html2canvas', source)
        self.assertNotIn('html2canvas', about_source)
        self.assertIn("static 'js/qrcode.min.js'", source)
        self.assertIn('window.QRCode', module_source)
        self.assertIn('const targetWidth = 960', module_source)
        self.assertIn('const targetHeight = 720', module_source)
        self.assertIn("if (blob === null)", module_source)
        self.assertIn("typeof ClipboardItem !== 'undefined'", module_source)
        self.assertIn('getOrCreateInstance(modalElement)', module_source)
        self.assertIn('container.remove()', module_source)
        self.assertIn('link?.remove()', module_source)
