import re
from pathlib import Path
from types import SimpleNamespace

from django.conf import settings
from django.contrib.messages import constants as message_constants
from django.contrib.auth.models import AnonymousUser, User
from django.core.paginator import Paginator
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

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

    def test_shell_exposes_responsive_accessible_navigation(self):
        response = self.client.get(reverse('index'))
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn('class="app-skip-link" href="#main-content"', content)
        self.assertIn('<nav class="navbar navbar-expand-xl navbar-dark app-navbar" aria-label="主导航">', content)
        self.assertIn('aria-controls="navbarNav"', content)
        self.assertIn('aria-expanded="false"', content)
        self.assertIn('aria-label="展开或收起导航菜单"', content)
        self.assertIn('aria-label="浏览记录"', content)
        self.assertRegex(
            content,
            re.compile(
                r'class="nav-link active"\s+href="/"\s+aria-current="page"',
            ),
        )
        self.assertIn('role="search"', content)
        self.assertIn('for="site-search">搜索分享或分享 ID</label>', content)
        self.assertIn('aria-label="提交搜索"', content)
        self.assertIn('<main id="main-content" class="app-main py-4" tabindex="-1">', content)
        self.assertIn(f'2010 - {timezone.localdate().year} SQUARE ENIX', content)

    def test_authenticated_shell_marks_my_content_as_current(self):
        self.client.force_login(self.author)

        response = self.client.get(reverse('my_shares'))
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertRegex(
            content,
            re.compile(
                r'class="nav-link active"\s+href="/my-shares/"\s+'
                r'aria-current="page"',
            ),
        )

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

    def test_my_reaction_pagination_preserves_active_tab(self):
        related_shares = []
        for index in range(13):
            related_shares.append(Share.objects.create(
                title=f'分页契约 {index}',
                strategy_code=f'[stgy:pagination-{index}]',
                author=self.author,
                visibility=Share.Visibility.PUBLIC,
                status=Share.Status.APPROVED,
            ))
        self.author.liked_shares.add(*related_shares)
        self.author.favorited_shares.add(*related_shares)
        self.client.force_login(self.author)

        for tab in ('likes', 'favorites'):
            with self.subTest(tab=tab):
                response = self.client.get(reverse('my_shares'), {'tab': tab})
                content = response.content.decode()

                self.assertEqual(response.status_code, 200)
                self.assertIn('aria-label="我的内容分页"', content)
                self.assertIn(f'?tab={tab}&amp;page=2', content)
                self.assertNotIn('href="?page=2"', content)


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

    def test_visit_history_uses_stable_dom_and_style_contracts(self):
        source = self.read_template('base.html')
        history_source = self.read_frontend('features/visit-history.ts')
        app_shell_source = self.read_frontend('styles/app-shell.css')

        self.assertIn('data-history-divider', source)
        self.assertRegex(source, re.compile(r'data-clear-history\s+disabled'))
        self.assertIn("listItem.dataset.historyItem = ''", history_source)
        self.assertIn("querySelectorAll('[data-history-item]')", history_source)
        self.assertIn('clearButton.disabled = history.length === 0', history_source)
        self.assertNotIn('header.nextElementSibling', history_source)
        self.assertNotIn('title.style.', history_source)
        self.assertNotIn('dateLabel.style.', history_source)
        self.assertIn('.app-history-item__title', app_shell_source)
        self.assertIn('.app-history-item__date', app_shell_source)

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
            "@import './bootstrap-adapter.css';",
            "@import './components.css';",
            "@import './app-shell.css';",
            "@import './share-preview.css';",
            "@import './empty-state.css';",
            "@import './feedback.css';",
            "@import './pagination.css';",
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
        app_shell_source = self.read_frontend('styles/app-shell.css')
        adapter_source = self.read_frontend('styles/bootstrap-adapter.css')
        self.assertIn('.registration-form #id_username', components_source)
        self.assertIn('.admin-tabs .nav-link', components_source)
        self.assertIn('.share-card {', components_source)
        self.assertIn('box-shadow: var(--app-shadow-sm);', components_source)
        self.assertIn('@media (prefers-reduced-motion: reduce)', components_source)
        self.assertNotIn('transition: all', components_source)
        self.assertNotRegex(
            components_source,
            re.compile(r'\.card\s*\{[^}]*overflow\s*:', re.DOTALL),
        )
        self.assertIn('--bs-card-border-radius:', adapter_source)
        self.assertIn('--bs-card-inner-border-radius:', adapter_source)
        self.assertIn('--bs-badge-font-size:', adapter_source)
        self.assertIn('.modal {', adapter_source)
        self.assertIn('.form-control:focus', adapter_source)
        self.assertNotIn('--bs-btn-bg:', adapter_source)
        self.assertNotIn('--bs-alert-bg:', adapter_source)
        self.assertIn('.app-navbar__actions', app_shell_source)
        self.assertIn('.app-footer__inner', app_shell_source)
        self.assertIn('@media (max-width: 1199.98px)', app_shell_source)
        self.assertIn('@media (prefers-reduced-motion: reduce)', app_shell_source)
        self.assertNotIn('style="min-width: 260px;', self.read_template('base.html'))
        self.assertIn(
            'class="registration-form"',
            self.read_template('shares/register.html'),
        )
        self.assertIn(
            'admin-tabs',
            self.read_template('shares/includes/admin_tabs.html'),
        )
        self.assertNotIn(
            'style="overflow: visible;"',
            self.read_template('shares/index.html'),
        )

        for template_path in (
            'shares/includes/share_cards.html',
            'shares/my_shares.html',
            'shares/user_public_profile.html',
            'shares/collection_detail.html',
            'shares/admin_review_list.html',
        ):
            with self.subTest(template=template_path):
                source = self.read_template(template_path)
                self.assertIn('share-card card-hover', source)
                self.assertNotRegex(
                    source,
                    re.compile(
                        r'class="[^"]*(?:card-hover[^"]*shadow-sm|shadow-sm[^"]*card-hover)',
                    ),
                )

        base_source = self.read_template('base.html')
        feedback_source = self.read_template('shares/includes/flash_messages.html')
        notify_source = self.read_frontend('core/notify.ts')
        self.assertIn("{% include 'shares/includes/flash_messages.html' %}", base_source)
        self.assertIn('app-notifications', feedback_source)
        self.assertIn('app-notification__message', feedback_source)
        self.assertIn('app-notification__message', notify_source)
        self.assertNotIn('messageText.style', notify_source)

    def test_pagination_component_preserves_query_parameters(self):
        request = RequestFactory().get(
            '/admin/logs/',
            {'tab': 'review', 'filter': 'open', 'page': '2'},
        )
        request.user = AnonymousUser()
        paginator = Paginator(range(30), 10)
        page_obj = paginator.get_page(2)
        content = render_to_string(
            'shares/includes/pagination.html',
            {'page_obj': page_obj, 'aria_label': '测试分页'},
            request=request,
        )

        self.assertIn('aria-label="测试分页"', content)
        self.assertIn('aria-current="page"', content)
        self.assertIn('rel="prev"', content)
        self.assertIn('rel="next"', content)
        self.assertIn('?tab=review&amp;filter=open&amp;page=1', content)
        self.assertIn('?tab=review&amp;filter=open&amp;page=3', content)

        first_page_content = render_to_string(
            'shares/includes/pagination.html',
            {'page_obj': paginator.get_page(1), 'aria_label': '测试分页'},
            request=request,
        )
        self.assertIn('aria-disabled="true"', first_page_content)

        large_page_content = render_to_string(
            'shares/includes/pagination.html',
            {
                'page_obj': Paginator(range(10_000), 10).get_page(500),
                'aria_label': '大列表分页',
            },
            request=request,
        )
        self.assertIn('省略部分页码', large_page_content)
        self.assertLessEqual(large_page_content.count('class="page-item'), 11)

    def test_home_uses_shared_bounded_pagination(self):
        source = self.read_template('shares/index.html')

        self.assertIn("shares/includes/pagination.html", source)
        self.assertIn("aria_label='首页分享分页'", source)
        self.assertNotIn('shares.paginator.page_range', source)
        self.assertNotIn('id="pageDropdown"', source)
        self.assertNotIn('href="?page=', source)

    def test_admin_log_pages_use_shared_pagination_component(self):
        for template_path in (
            'shares/admin_log_list.html',
            'shares/admin_review_logs.html',
            'shares/admin_report_logs.html',
        ):
            with self.subTest(template=template_path):
                source = self.read_template(template_path)
                self.assertIn(
                    "{% include 'shares/includes/pagination.html' with page_obj=logs",
                    source,
                )
                self.assertNotIn('logs.paginator.page_range', source)

        my_shares_source = self.read_template('shares/my_shares.html')
        self.assertIn(
            "{% include 'shares/includes/pagination.html' with page_obj=shares",
            my_shares_source,
        )
        self.assertNotIn('href="?page=', my_shares_source)

    def test_common_template_events_use_data_contracts(self):
        preview_template = self.read_template('shares/includes/share_preview.html')
        self.assertIn('data-preview-frame', preview_template)
        self.assertIn('data-preview-image', preview_template)
        self.assertIn('data-preview-loading', preview_template)
        self.assertNotIn('aria-busy="true"', preview_template)
        self.assertIn('share-preview__image', preview_template)
        self.assertIn('share-preview__skeleton', preview_template)
        self.assertIn('aria-hidden="true"', preview_template)
        self.assertNotIn('spinner-border', preview_template)
        self.assertNotIn('linear-gradient(135deg, #667eea', preview_template)
        self.assertNotIn(
            'object-fit: cover; width: 100%; height: 100%;',
            preview_template,
        )

        call_sites = (
            ('shares/includes/share_cards.html', "share=share preview_variant='standard'"),
            ('shares/my_shares.html', "share=share preview_variant='management'"),
            ('shares/user_public_profile.html', "share=share preview_variant='standard'"),
            ('shares/collection_detail.html', "share=item.share preview_variant='standard'"),
            ('shares/admin_review_list.html', "share=share preview_variant='review'"),
        )
        for template_path, contract in call_sites:
            with self.subTest(template=template_path):
                source = self.read_template(template_path)
                self.assertIn(
                    "{% include 'shares/includes/share_preview.html' with "
                    f'{contract} only %}}',
                    source,
                )
                self.assertNotIn('data-preview-frame', source)

        collection_source = self.read_template('shares/collection_detail.html')
        review_source = self.read_template('shares/admin_review_list.html')
        self.assertIn('?collection_id={{ collection.id }}', collection_source)
        self.assertNotIn('share-preview__link', review_source)

        self.assertIn(
            'data-submit-on-change',
            self.read_template('shares/index.html'),
        )
        self.assertIn(
            'data-confirm-message',
            self.read_template('shares/collection_detail.html'),
        )
        preview_source = self.read_frontend('features/preview-images.ts')
        preview_styles = self.read_frontend('styles/share-preview.css')
        controls_source = self.read_frontend('features/form-controls.ts')
        self.assertIn("image.addEventListener('load'", preview_source)
        self.assertIn("image.addEventListener('error'", preview_source)
        self.assertIn("document.addEventListener('htmx:load'", preview_source)
        self.assertIn("setAttribute('aria-busy', 'true')", preview_source)
        self.assertIn("frame.setAttribute('aria-busy', 'false')", preview_source)
        self.assertIn(".share-preview[aria-busy='true']", preview_styles)
        self.assertIn('@media (prefers-reduced-motion: reduce)', preview_styles)
        self.assertIn('var(--app-z-preview-meta)', preview_styles)
        self.assertIn('form?.requestSubmit()', controls_source)
        self.assertIn('window.confirm(message)', controls_source)

    def test_share_preview_variants_render_expected_metadata(self):
        share = SimpleNamespace(
            title='预览 & "标题"',
            strategy_code='[stgy:a&"b]',
            is_spoiler=True,
            is_nsfw=True,
            category='combat',
            is_original=True,
            views=123,
            copies=456,
            status='pending',
            visibility='private',
        )

        standard = render_to_string(
            'shares/includes/share_preview.html',
            {'share': share, 'preview_variant': 'standard'},
        )
        management = render_to_string(
            'shares/includes/share_preview.html',
            {'share': share, 'preview_variant': 'management'},
        )
        review = render_to_string(
            'shares/includes/share_preview.html',
            {'share': share, 'preview_variant': 'review'},
        )

        self.assertIn('alt="预览 &amp; &quot;标题&quot; 的战术板预览"', standard)
        self.assertIn('/n/board/[stgy:a&amp;&quot;b]', standard)
        self.assertNotIn('<script', standard)
        self.assertIn('可能令人不适', standard)
        self.assertNotIn('可能包含剧透', standard)
        self.assertIn('点击查看详情', standard)
        self.assertIn('bi bi-eye"></i> 123', standard)
        self.assertIn('bi bi-clipboard"></i> 456', standard)
        self.assertNotIn('私有', standard)

        self.assertIn('待审核', management)
        self.assertIn('私有', management)
        self.assertIn('bi bi-eye"></i> 123', management)

        self.assertIn('share-preview__warning--static', review)
        self.assertIn('待审核', review)
        self.assertNotIn('点击查看详情', review)
        self.assertNotIn('bi bi-eye"></i> 123', review)
        self.assertNotIn('bi bi-clipboard"></i> 456', review)

    def test_empty_state_component_is_escaped_and_reused(self):
        content = render_to_string(
            'shares/includes/empty_state.html',
            {
                'icon': 'bi-inbox',
                'title': '<script>alert(1)</script>',
                'message': '没有内容 & 请稍后再试',
                'action_url': '/create/?next=a&b=1',
                'action_label': '创建内容',
                'action_icon': 'bi-plus-circle',
            },
        )

        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', content)
        self.assertNotIn('<script>', content)
        self.assertIn('没有内容 &amp; 请稍后再试', content)
        self.assertIn('href="/create/?next=a&amp;b=1"', content)
        self.assertIn('aria-hidden="true"', content)

        for template_path in (
            'shares/includes/share_cards.html',
            'shares/my_shares.html',
            'shares/user_public_profile.html',
            'shares/collection_detail.html',
        ):
            with self.subTest(template=template_path):
                source = self.read_template(template_path)
                self.assertIn(
                    "{% include 'shares/includes/empty_state.html' with ",
                    source,
                )

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
