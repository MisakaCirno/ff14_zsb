import json
import re
from html.parser import HTMLParser
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


class _InteractionMarkupProbe(HTMLParser):
    _VOID_TAGS = {
        'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
        'meta', 'param', 'source', 'track', 'wbr',
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self._stack = []

    def handle_starttag(self, tag, attrs):
        element = {
            'tag': tag,
            'attrs': dict(attrs),
            'ancestors': tuple(self._stack),
        }
        self.elements.append(element)
        if tag not in self._VOID_TAGS:
            self._stack.append(element)

    def handle_startendtag(self, tag, attrs):
        self.elements.append({
            'tag': tag,
            'attrs': dict(attrs),
            'ancestors': tuple(self._stack),
        })

    def handle_endtag(self, tag):
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index]['tag'] == tag:
                del self._stack[index:]
                break

    def matching(self, *, tag=None, attribute=None, value=None):
        matches = self.elements
        if tag is not None:
            matches = [item for item in matches if item['tag'] == tag]
        if attribute is not None:
            matches = [
                item for item in matches
                if attribute in item['attrs']
                and (value is None or item['attrs'][attribute] == value)
            ]
        return matches


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
        self.assertIn('<nav class="navbar navbar-expand-xl navbar-light app-navbar" aria-label="主导航">', content)
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
        self.assertIn('aria-label="移动端主导航"', content)
        self.assertIn('for="mobile-site-search">搜索分享或分享 ID</label>', content)
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
        self.assertIn('data-share-card', content)
        self.assertIn('data-share-interaction="like"', content)
        self.assertIn('data-share-interaction="favorite"', content)
        self.assertIn('aria-label="点赞，当前 0 个点赞"', content)
        self.assertIn('aria-label="收藏，当前 0 个收藏"', content)
        self.assertGreaterEqual(content.count('aria-pressed="false"'), 2)
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
        self.assertIn('data-share-interaction="like"', content)
        self.assertIn('data-share-interaction="favorite"', content)
        self.assertNotIn('toggleLike()', content)
        self.assertNotIn('toggleFavorite()', content)
        self.assertNotIn('function toggleLike', content)
        self.assertNotIn('function toggleFavorite', content)

    def test_reaction_forms_are_external_unique_and_referenceable(self):
        second_share = Share.objects.create(
            title='第二个表单契约',
            strategy_code='[stgy:second-form-contract]',
            author=self.author,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )
        self.client.force_login(self.author)
        cases = (
            (
                self.client.get(reverse('index')),
                'card',
                reverse('index'),
                {
                    f'share-interaction-form-card-{self.share.share_id}',
                    f'share-interaction-form-card-{second_share.share_id}',
                },
            ),
            (
                self.client.get(
                    reverse('share_detail', args=[self.share.share_id]),
                ),
                'detail',
                self.share.get_absolute_url(),
                {f'share-interaction-form-detail-{self.share.share_id}'},
            ),
        )

        for response, fragment, expected_next, expected_form_ids in cases:
            with self.subTest(fragment=fragment):
                probe = _InteractionMarkupProbe()
                probe.feed(response.content.decode())
                expected_form_id = (
                    f'share-interaction-form-{fragment}-{self.share.share_id}'
                )
                forms = probe.matching(
                    tag='form',
                    attribute='data-share-interaction-form',
                )
                form_ids = [form['attrs'].get('id') for form in forms]

                self.assertEqual(set(form_ids), expected_form_ids)
                self.assertEqual(len(form_ids), len(set(form_ids)))
                form = next(
                    item for item in forms
                    if item['attrs'].get('id') == expected_form_id
                )
                self.assertEqual(form['attrs'].get('method'), 'post')
                self.assertNotIn(
                    'form',
                    {ancestor['tag'] for ancestor in form['ancestors']},
                )
                ancestor_classes = {
                    class_name
                    for ancestor in form['ancestors']
                    for class_name in ancestor['attrs'].get('class', '').split()
                }
                self.assertNotIn('btn-group', ancestor_classes)
                self.assertNotIn('browse-card__actions', ancestor_classes)

                form_inputs = [
                    item for item in probe.matching(tag='input')
                    if form in item['ancestors']
                ]
                self.assertTrue(any(
                    item['attrs'].get('name') == 'csrfmiddlewaretoken'
                    and item['attrs'].get('value')
                    for item in form_inputs
                ))
                self.assertTrue(any(
                    item['attrs'].get('type') == 'hidden'
                    and item['attrs'].get('name') == 'next'
                    and item['attrs'].get('value') == expected_next
                    for item in form_inputs
                ))

                expected_actions = {
                    f'btn-like-{self.share.share_id}': reverse(
                        'toggle_like', args=[self.share.share_id],
                    ),
                    f'btn-favorite-{self.share.share_id}': reverse(
                        'toggle_favorite', args=[self.share.share_id],
                    ),
                }
                for button_id, action in expected_actions.items():
                    button = probe.matching(
                        tag='button', attribute='id', value=button_id,
                    )[0]
                    self.assertEqual(button['attrs']['type'], 'submit')
                    self.assertEqual(button['attrs']['form'], expected_form_id)
                    self.assertEqual(button['attrs']['formaction'], action)
                    self.assertEqual(button['attrs']['name'], 'target_state')
                    self.assertEqual(button['attrs']['value'], 'active')
                    self.assertNotIn(
                        'form',
                        {
                            ancestor['tag']
                            for ancestor in button['ancestors']
                        },
                    )
                    if fragment == 'card':
                        self.assertIn(
                            'btn-group',
                            button['ancestors'][-1]['attrs']
                            .get('class', '').split(),
                        )

    def test_reaction_forms_render_only_with_authenticated_buttons(self):
        anonymous_pages = (
            self.client.get(reverse('index')),
            self.client.get(
                reverse('share_detail', args=[self.share.share_id]),
            ),
        )
        for response in anonymous_pages:
            probe = _InteractionMarkupProbe()
            probe.feed(response.content.decode())
            self.assertFalse(probe.matching(
                tag='form', attribute='data-share-interaction-form',
            ))

        self.client.force_login(self.author)
        non_interactive_variants = (
            self.client.get(reverse('my_shares')),
            self.client.get(reverse(
                'user_public_profile', args=[self.author.username],
            )),
        )
        for response in non_interactive_variants:
            probe = _InteractionMarkupProbe()
            probe.feed(response.content.decode())
            self.assertFalse(probe.matching(
                tag='form', attribute='data-share-interaction-form',
            ))

    def test_hx_reaction_fragments_keep_deterministic_form_references(self):
        self.client.force_login(self.author)
        cases = (
            ('toggle_like', 'card'),
            ('toggle_favorite', 'detail'),
        )

        for endpoint, fragment in cases:
            with self.subTest(endpoint=endpoint, fragment=fragment):
                response = self.client.post(
                    reverse(endpoint, args=[self.share.share_id])
                    + f'?fragment={fragment}',
                    {'target_state': 'active'},
                    HTTP_HX_REQUEST='true',
                )
                probe = _InteractionMarkupProbe()
                probe.feed(response.content.decode())
                buttons = probe.matching(tag='button', attribute='form')

                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(buttons), 1)
                self.assertEqual(
                    buttons[0]['attrs']['form'],
                    f'share-interaction-form-{fragment}-{self.share.share_id}',
                )
                self.assertEqual(buttons[0]['attrs']['hx-target'], 'this')
                self.assertEqual(buttons[0]['attrs']['hx-swap'], 'outerHTML')
                self.assertFalse(probe.matching(tag='form'))

    def test_browse_and_my_reaction_forms_use_canonical_safe_return_urls(self):
        self.share.likes.add(self.author)
        self.share.favorites.add(self.author)
        self.client.force_login(self.author)

        browse_cases = (
            (
                reverse('index'),
                {'sort': 'likes', 'page': '999', 'continuation': '1'},
                '/?sort=likes',
            ),
            (
                reverse('search'),
                {'q': '前端', 'page': '999', 'partial': 'ignored'},
                '/search/?q=%E5%89%8D%E7%AB%AF',
            ),
        )
        for path, query, expected_next in browse_cases:
            with self.subTest(path=path):
                response = self.client.get(path, query)
                probe = _InteractionMarkupProbe()
                probe.feed(response.content.decode())
                interaction_forms = probe.matching(
                    tag='form', attribute='data-share-interaction-form',
                )
                next_inputs = probe.matching(
                    tag='input', attribute='name', value='next',
                )
                self.assertEqual(
                    [
                        item['attrs']['value'] for item in next_inputs
                        if any(
                            form in item['ancestors']
                            for form in interaction_forms
                        )
                    ],
                    [expected_next],
                )

        for tab in ('likes', 'favorites'):
            with self.subTest(tab=tab):
                response = self.client.get(reverse('my_shares'), {
                    'tab': tab,
                    'page': '999',
                    'order': 'desc',
                    'source': 'untrusted-extra-state',
                })
                probe = _InteractionMarkupProbe()
                probe.feed(response.content.decode())
                interaction_forms = probe.matching(
                    tag='form', attribute='data-share-interaction-form',
                )
                next_inputs = probe.matching(
                    tag='input', attribute='name', value='next',
                )
                self.assertEqual(
                    [
                        item['attrs']['value'] for item in next_inputs
                        if any(
                            form in item['ancestors']
                            for form in interaction_forms
                        )
                    ],
                    [f'{reverse("my_shares")}?tab={tab}&page=1'],
                )

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

    def test_my_reaction_sections_refresh_from_authoritative_resolved_page(self):
        related_shares = [
            Share.objects.create(
                title=f'互动刷新契约 {index}',
                strategy_code=f'[stgy:reaction-refresh-{index}]',
                author=self.author,
                visibility=Share.Visibility.PUBLIC,
                status=Share.Status.APPROVED,
            )
            for index in range(13)
        ]
        self.author.liked_shares.add(*related_shares)
        self.author.favorited_shares.add(*related_shares)
        self.client.force_login(self.author)

        for tab, event_name in (
            ('likes', 'share-like-removed'),
            ('favorites', 'share-favorite-removed'),
        ):
            with self.subTest(tab=tab):
                response = self.client.get(reverse('my_shares'), {
                    'tab': tab,
                    'page': '999',
                })
                probe = _InteractionMarkupProbe()
                probe.feed(response.content.decode())
                sections = probe.matching(
                    tag='section',
                    attribute='data-my-content-shares',
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(sections), 1)
                attrs = sections[0]['attrs']
                self.assertEqual(attrs['data-my-content-section'], tab)
                self.assertEqual(
                    attrs['hx-get'],
                    f'{reverse("my_shares")}?tab={tab}&page=2',
                )
                self.assertEqual(attrs['hx-trigger'], f'{event_name} from:body')
                self.assertEqual(attrs['hx-select'], '[data-my-content-shares]')
                self.assertEqual(attrs['hx-target'], 'this')
                self.assertEqual(attrs['hx-swap'], 'outerHTML')
                self.assertEqual(attrs['hx-sync'], 'this:replace')
                heading = probe.matching(
                    tag='h2', attribute='id', value='my-content-section-title',
                )[0]
                self.assertEqual(heading['attrs']['tabindex'], '-1')

        for query in ({}, {'tab': 'collections'}):
            with self.subTest(non_reaction_tab=query.get('tab', 'my_shares')):
                response = self.client.get(reverse('my_shares'), query)
                content = response.content.decode()
                self.assertNotIn('share-like-removed from:body', content)
                self.assertNotIn('share-favorite-removed from:body', content)

    def test_my_content_share_variants_keep_actions_separate(self):
        reactor = User.objects.create_user(username='reactor', password='password123')
        self.share.likes.add(reactor)
        self.share.favorites.add(reactor)
        self.share.visibility = Share.Visibility.PRIVATE
        self.share.status = Share.Status.REJECTED
        self.share.review_feedback = '<script>请修正审核问题</script>'
        self.share.restriction_state = Share.RestrictionState.REVIEW_REJECTED
        self.share.restriction_reason = '<script>请修正审核问题</script>'
        self.share.restricted_at = timezone.now()
        self.share.save(update_fields=[
            'visibility',
            'status',
            'review_feedback',
            'restriction_state',
            'restriction_reason',
            'restricted_at',
        ])
        self.client.force_login(self.author)

        management = self.client.get(reverse('my_shares'))
        management_content = management.content.decode()

        self.assertEqual(management.status_code, 200)
        self.assertIn('data-share-card-variant="management"', management_content)
        self.assertIn('data-managed-share', management_content)
        self.assertIn('审核失败', management_content)
        self.assertIn('私有', management_content)
        self.assertIn('内容受限：', management_content)
        self.assertIn('&lt;script&gt;请修正审核问题&lt;/script&gt;', management_content)
        self.assertIn('aria-label="1 个点赞"', management_content)
        self.assertIn('aria-label="1 个收藏"', management_content)
        self.assertIn(reverse('edit_share', args=[self.share.share_id]), management_content)
        self.assertIn(reverse('delete_share', args=[self.share.share_id]), management_content)
        self.assertNotIn('data-copy-strategy', management_content)
        self.assertNotIn('?fragment=card', management_content)

        self.share.visibility = Share.Visibility.PUBLIC
        self.share.status = Share.Status.APPROVED
        self.share.review_feedback = ''
        self.share.restriction_state = Share.RestrictionState.CLEAR
        self.share.restriction_reason = ''
        self.share.restricted_at = None
        self.share.save(update_fields=[
            'visibility',
            'status',
            'review_feedback',
            'restriction_state',
            'restriction_reason',
            'restricted_at',
        ])
        self.author.liked_shares.add(self.share)

        browse = self.client.get(reverse('my_shares'), {'tab': 'likes'})
        browse_content = browse.content.decode()

        self.assertEqual(browse.status_code, 200)
        self.assertIn('data-share-card-variant="browse"', browse_content)
        self.assertIn('data-copy-strategy', browse_content)
        self.assertIn(
            f'hx-post="/share/{self.share.share_id}/like/?fragment=card"',
            browse_content,
        )
        self.assertIn(
            f'hx-post="/share/{self.share.share_id}/favorite/?fragment=card"',
            browse_content,
        )
        self.assertNotIn('data-managed-share', browse_content)
        self.assertNotIn(
            reverse('edit_share', args=[self.share.share_id]),
            browse_content,
        )
        self.assertNotIn(
            reverse('delete_share', args=[self.share.share_id]),
            browse_content,
        )


class FrontendTemplateSourceTests(SimpleTestCase):
    def read_template(self, relative_path):
        return (Path(settings.BASE_DIR) / 'templates' / relative_path).read_text(
            encoding='utf-8',
        )

    def read_frontend(self, relative_path):
        return (Path(settings.BASE_DIR) / 'frontend' / 'src' / relative_path).read_text(
            encoding='utf-8',
        )

    def read_project_file(self, relative_path):
        return (Path(settings.BASE_DIR) / relative_path).read_text(encoding='utf-8')

    def assert_css_rule_contains(self, source, selector, declarations):
        match = re.search(
            rf'(?m)^{re.escape(selector)}\s*\{{(?P<body>[^}}]*)\}}',
            source,
        )
        self.assertIsNotNone(match, f'Missing CSS rule: {selector}')
        body = match.group('body')
        for declaration in declarations:
            with self.subTest(selector=selector, declaration=declaration):
                self.assertIn(declaration, body)
        return body

    def assert_css_selector_group_contains(self, source, selector, declarations):
        body = None
        for match in re.finditer(
            r'(?P<selectors>[^{}]+)\{(?P<body>[^{}]*)\}',
            source,
        ):
            selectors = {
                item.strip()
                for item in match.group('selectors').split(',')
            }
            if selector in selectors:
                body = match.group('body')
                if all(declaration in body for declaration in declarations):
                    break
                body = None

        self.assertIsNotNone(
            body,
            f'Missing CSS selector group for {selector} with {declarations}',
        )
        return body

    def test_shared_copy_buttons_do_not_use_inline_handlers(self):
        card_source = self.read_template('shares/includes/share_card.html')
        self.assertIn('data-copy-strategy', card_source)
        self.assertNotIn('copyStrategyCode(', card_source)

        my_content_source = self.read_template('shares/my_shares.html')
        self.assertIn(
            "{% include 'shares/includes/share_card.html' with ",
            my_content_source,
        )
        self.assertNotIn('data-copy-strategy', my_content_source)
        self.assertNotIn('copyStrategyCode(', my_content_source)

        for template_path in (
            'shares/includes/share_cards.html',
            'shares/user_public_profile.html',
        ):
            with self.subTest(template=template_path):
                source = self.read_template(template_path)
                self.assertIn(
                    "{% include 'shares/includes/share_card.html' with ",
                    source,
                )
                self.assertNotIn('copyStrategyCode(', source)

    def test_public_share_cards_use_explicit_accessible_variants(self):
        card_source = self.read_template('shares/includes/share_card.html')
        list_source = self.read_template('shares/includes/share_cards.html')
        profile_source = self.read_template('shares/user_public_profile.html')
        my_content_source = self.read_template('shares/my_shares.html')
        like_source = self.read_template('shares/includes/like_button.html')
        favorite_source = self.read_template('shares/includes/favorite_button.html')
        card_styles = self.read_frontend('styles/share-card.css')
        action_source = self.read_frontend('features/share-actions.ts')
        copy_source = self.read_frontend('features/share-copy.ts')

        self.assertIn("card_variant='browse' viewer=user", list_source)
        self.assertIn('spoiler_preference=spoiler_preference', list_source)
        self.assertIn('nsfw_preference=nsfw_preference', list_source)
        self.assertIn('login_return_url=share_cards_return_url', list_source)
        self.assertIn("card_variant='profile' viewer=user only", profile_source)
        self.assertIn("card_variant='management' only", my_content_source)
        self.assertIn("card_variant='browse' viewer=user only", my_content_source)
        self.assertIn('<article class="card h-100 share-card card-hover browse-card', card_source)
        self.assertIn('aria-labelledby="share-card-title-', card_source)
        self.assertIn('data-share-card', card_source)
        self.assertIn('data-share-card-variant=', card_source)
        self.assertIn('data-managed-share', card_source)
        self.assertIn("{% if card_variant == 'management' %}", card_source)
        self.assertIn("{% elif card_variant == 'profile' %}", card_source)
        self.assertIn("{% elif card_variant == 'browse' %}", card_source)
        self.assertIn('aria-label="分享操作"', card_source)
        self.assertIn('aria-label="互动操作"', card_source)
        self.assertIn('class="management-card__actions"', card_source)
        self.assertIn("{% url 'edit_share' share.share_id %}", card_source)
        self.assertIn("{% url 'delete_share' share.share_id %}", card_source)
        self.assertNotIn('style="', card_source)

        for source, label in (
            (like_source, '点赞，当前'),
            (favorite_source, '收藏，当前'),
        ):
            self.assertIn(f'aria-label="{label}', source)
            self.assertIn('aria-pressed="{% if ', source)
            self.assertIn('aria-hidden="true"', source)

        self.assertIn('.browse-card:focus-within', card_styles)
        self.assertIn('.browse-card__actions', card_styles)
        self.assertIn('.management-card__actions', card_styles)
        self.assertIn('@container browse-card (max-width: 18rem)', card_styles)
        self.assertIn('@container browse-card (max-width: 15rem)', card_styles)
        self.assertIn("import { performShareCopy } from './share-copy'", action_source)
        self.assertIn("icon.setAttribute('aria-hidden', 'true')", copy_source)

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
        self.assertIn(
            "import { initializeShareInteractions } from './features/share-interactions'",
            main_source,
        )
        self.assertIn('initializeShareInteractions()', main_source)

    def test_visit_history_uses_stable_dom_and_style_contracts(self):
        source = self.read_template('base.html')
        history_source = self.read_frontend('features/visit-history.ts')
        app_shell_source = self.read_frontend('styles/app-shell.css')

        self.assertIn('data-history-divider', source)
        self.assertRegex(source, re.compile(r'data-clear-history\s+disabled'))
        self.assertIn("listItem.dataset.historyItem = ''", history_source)
        self.assertIn("querySelectorAll('[data-history-item]')", history_source)
        self.assertIn('clearButton.disabled = history.length === 0', history_source)
        self.assertIn(
            'link.href = `/s/${encodeURIComponent(item.id)}`',
            history_source,
        )
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
            "@import './browse-page.css';",
            "@import './share-preview.css';",
            "@import './share-card.css';",
            "@import './collection-card.css';",
            "@import './public-profile.css';",
            "@import './my-content-page.css';",
            "@import './account-page.css';",
            "@import './site-message-page.css';",
            "@import './static-page.css';",
            "@import './collection-page.css';",
            "@import './share-editor-page.css';",
            "@import './share-detail-page.css';",
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
        self.assertIn('--app-radius-control:', tokens_source)
        self.assertIn('--app-radius-surface:', tokens_source)
        self.assertIn('--app-shadow-surface:', tokens_source)
        self.assertIn('--app-shadow-floating:', tokens_source)
        self.assertIn('--app-text-page-title:', tokens_source)
        self.assertIn('--app-text-section-title:', tokens_source)
        self.assertIn('--app-focus-ring-color:', tokens_source)
        self.assertIn('--app-motion-normal:', tokens_source)

        components_source = self.read_frontend('styles/components.css')
        app_shell_source = self.read_frontend('styles/app-shell.css')
        account_page_source = self.read_frontend('styles/account-page.css')
        adapter_source = self.read_frontend('styles/bootstrap-adapter.css')
        for shared_selector in (
            '.auth-page {',
            '.account-form {',
            '.account-field {',
            '.account-error-summary {',
            '.account-settings-page {',
            '.account-settings-nav {',
        ):
            self.assertIn(shared_selector, account_page_source)
        self.assertIn('container-type: inline-size;', account_page_source)
        self.assertIn('@container (max-width: 48rem)', account_page_source)
        self.assertIn(
            '@media (prefers-reduced-motion: reduce)',
            account_page_source,
        )
        self.assertNotIn('#id_', account_page_source)
        self.assertIn('.admin-tabs .nav-link', components_source)
        self.assertIn('.share-card {', components_source)
        self.assertIn('box-shadow: var(--app-shadow-surface);', components_source)
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
        self.assertNotIn('--bs-alert-bg:', adapter_source)
        self.assertIn('.app-navbar__actions', app_shell_source)
        self.assertIn('.app-footer__inner', app_shell_source)
        self.assertIn('container-name: app-navbar;', app_shell_source)
        self.assertIn('container-type: inline-size;', app_shell_source)
        self.assertIn('@container app-navbar (max-width: 74rem)', app_shell_source)
        self.assertRegex(
            app_shell_source,
            re.compile(
                r'\.app-navbar\.navbar-expand-xl > \.app-navbar__container\s*'
                r'\{\s*flex-wrap:\s*wrap;',
            ),
        )
        self.assertRegex(
            app_shell_source,
            re.compile(
                r'\.app-navbar\.navbar-expand-xl \.navbar-collapse\s*\{\s*'
                r'width:\s*100%;\s*flex-basis:\s*100%;',
            ),
        )
        self.assertIn(
            '.app-navbar.navbar-expand-xl .navbar-collapse:not(.show)',
            app_shell_source,
        )
        self.assertRegex(
            app_shell_source,
            re.compile(
                r'\.app-navbar\.navbar-expand-xl '
                r'\.navbar-collapse\.collapsing,\s*'
                r'\.app-navbar\.navbar-expand-xl '
                r'\.navbar-collapse\.show\s*\{\s*'
                r'display:\s*block\s*!important;',
            ),
        )
        self.assertIn('@media (max-width: 1199.98px)', app_shell_source)
        self.assertLess(
            app_shell_source.index('@media (max-width: 1199.98px)'),
            app_shell_source.index('@container app-navbar (max-width: 74rem)'),
        )
        self.assertIn('@media (prefers-reduced-motion: reduce)', app_shell_source)
        self.assertNotIn('style="min-width: 260px;', self.read_template('base.html'))
        login_source = self.read_template('shares/login.html')
        register_source = self.read_template('shares/register.html')
        profile_source = self.read_template('shares/profile_edit.html')
        password_source = self.read_template('shares/password_change.html')
        for page_name, source in (
            ('login', login_source),
            ('register', register_source),
        ):
            with self.subTest(account_page=page_name):
                self.assertIn(f'data-auth-page="{page_name}"', source)
                self.assertIn('data-account-form', source)
                self.assertIn(
                    'shares/includes/account_form_field.html',
                    source,
                )

        for page_name, source in (
            ('profile', profile_source),
            ('password', password_source),
        ):
            with self.subTest(account_page=page_name):
                self.assertIn(
                    f'data-account-settings-page="{page_name}"',
                    source,
                )
                self.assertIn('data-account-form', source)
                self.assertIn(
                    'shares/includes/account_settings_nav.html',
                    source,
                )
                self.assertIn(
                    'shares/includes/account_form_field.html',
                    source,
                )

        self.assertIn(
            'data-account-field=',
            self.read_template('shares/includes/account_form_field.html'),
        )
        self.assertIn(
            'data-account-error-summary',
            self.read_template('shares/includes/account_error_summary.html'),
        )
        self.assertIn(
            'data-account-settings-nav',
            self.read_template('shares/includes/account_settings_nav.html'),
        )

        account_feature_source = self.read_frontend('features/account-forms.ts')
        main_entry_source = self.read_frontend('main.ts')
        self.assertIn("'[data-account-error-summary]'", account_feature_source)
        self.assertIn('initializeAccountForms()', main_entry_source)
        self.assertIn(
            'moderation-tabs',
            self.read_template('shares/includes/admin_tabs.html'),
        )
        self.assertNotIn(
            'style="overflow: visible;"',
            self.read_template('shares/index.html'),
        )

        for template_path in (
            'shares/includes/share_card.html',
            'shares/includes/collection_item_card.html',
            'shares/includes/moderation_review_card.html',
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

        self.assertIn(
            "{% include 'shares/includes/share_card.html' with ",
            self.read_template('shares/my_shares.html'),
        )

        for template_path in (
            'shares/includes/share_cards.html',
            'shares/user_public_profile.html',
        ):
            with self.subTest(template=template_path):
                self.assertIn(
                    "{% include 'shares/includes/share_card.html' with ",
                    self.read_template(template_path),
                )

        base_source = self.read_template('base.html')
        feedback_source = self.read_template('shares/includes/flash_messages.html')
        notify_source = self.read_frontend('core/notify.ts')
        self.assertIn("{% include 'shares/includes/flash_messages.html' %}", base_source)
        self.assertIn('app-notifications', feedback_source)
        self.assertIn('app-notification__message', feedback_source)
        self.assertIn('app-notification__message', notify_source)
        self.assertNotIn('messageText.style', notify_source)

    def test_shared_visual_components_are_reused_across_pages(self):
        components_source = self.read_frontend('styles/components.css')
        adapter_source = self.read_frontend('styles/bootstrap-adapter.css')
        page_header_source = self.read_template(
            'shares/includes/page_header.html',
        )

        for selector in (
            '.ui-page-header {',
            '.ui-panel {',
            '.ui-section-header {',
            '.ui-segmented-nav {',
            '.ui-icon-tile {',
        ):
            self.assertIn(selector, components_source)

        for bootstrap_radius in (
            '--bs-border-radius: var(--app-radius-control);',
            '--bs-border-radius-lg: var(--app-radius-surface);',
            '--bs-border-radius-pill: var(--app-radius-pill);',
        ):
            self.assertIn(bootstrap_radius, adapter_source)

        self.assertIn('class="ui-page-header ui-page-header--icon', page_header_source)
        self.assertIn('class="ui-page-title"', page_header_source)
        self.assertFalse(
            (Path(settings.BASE_DIR) / 'templates' / 'shares' / 'includes'
             / 'moderation_page_header.html').exists(),
        )

        template_contracts = {
            'shares/my_shares.html': (
                'ui-page-header my-content-hero',
                'ui-section-header my-content-section__header',
                'ui-segmented-nav my-content-nav',
            ),
            'shares/profile_edit.html': (
                'ui-page-header account-settings-hero',
                'ui-panel account-panel',
            ),
            'shares/site_message_list.html': (
                'ui-page-header message-center-hero',
                'ui-segmented-nav message-center-nav',
            ),
            'shares/detail.html': (
                'ui-page-header share-detail-hero',
                'ui-panel share-detail-panel',
            ),
        }
        for template_path, hooks in template_contracts.items():
            source = self.read_template(template_path)
            for hook in hooks:
                with self.subTest(template=template_path, hook=hook):
                    self.assertIn(hook, source)

        about_source = self.read_template('about.html')
        not_found_source = self.read_template('404.html')
        static_page_source = self.read_frontend('styles/static-page.css')
        self.assertIn("shares/includes/page_header.html", about_source)
        self.assertIn('class="ui-panel"', about_source)
        self.assertIn('class="ui-panel not-found"', not_found_source)
        self.assertNotIn('style="', about_source + not_found_source)
        self.assertNotIn('javascript:', not_found_source)
        self.assertIn('.static-page {', static_page_source)

    def test_frontend_verification_runs_design_and_contrast_checkers(self):
        package = json.loads(self.read_project_file('frontend/package.json'))
        scripts = package['scripts']

        self.assertEqual(
            scripts.get('check:design'),
            'node scripts/check-design-system.mjs',
        )
        self.assertEqual(
            scripts.get('check:contrast'),
            'node scripts/check-color-contrast.mjs',
        )
        self.assertEqual(
            scripts.get('verify'),
            'npm run test && npm run check:design && npm run check:contrast '
            '&& npm run typecheck && npm run lint && npm run build',
        )
        self.assertTrue(
            (Path(settings.BASE_DIR) / 'frontend' / 'scripts'
             / 'check-color-contrast.mjs').is_file(),
        )
        self.assertTrue(
            (Path(settings.BASE_DIR) / 'frontend' / 'scripts'
             / 'check-design-system.mjs').is_file(),
        )

    def test_color_tokens_cover_interactive_and_shell_contexts(self):
        tokens_source = self.read_frontend('styles/tokens.css')
        required_tokens = (
            '--app-color-on-strong:',
            '--app-color-on-bright:',
            '--app-color-control-border:',
            '--app-color-warning-text:',
            '--app-color-info-text:',
            '--app-color-disabled-text:',
            '--app-color-disabled-bg:',
            '--app-color-disabled-border:',
            '--app-color-shell-control-border:',
            '--app-color-shell-focus-ring:',
            '--app-color-shell-notification:',
            '--app-color-danger-on-dark:',
            '--app-color-image-focus-inner:',
            '--app-color-image-focus-outer:',
            '--app-color-dialog-backdrop:',
            '--app-color-media-canvas:',
            '--app-color-media-veil:',
        )
        for token in required_tokens:
            with self.subTest(token=token):
                self.assertIn(token, tokens_source)

        for semantic_color in (
            'primary',
            'secondary',
            'success',
            'warning',
            'danger',
            'info',
        ):
            with self.subTest(semantic_color=semantic_color):
                self.assertIn(
                    f'--app-color-{semantic_color}-rgb:',
                    tokens_source,
                )
                self.assertIn(
                    f'--app-color-{semantic_color}-hover:',
                    tokens_source,
                )
                self.assertIn(
                    f'--app-color-{semantic_color}-active:',
                    tokens_source,
                )
                self.assertIn(
                    f'--app-color-{semantic_color}-soft:',
                    tokens_source,
                )

    def test_bootstrap_adapter_maps_primary_button_states_to_app_tokens(self):
        adapter_source = self.read_frontend('styles/bootstrap-adapter.css')

        self.assert_css_rule_contains(
            adapter_source,
            '.btn-primary',
            (
                '--bs-btn-color: var(--app-color-on-strong);',
                '--bs-btn-bg: var(--app-color-primary);',
                '--bs-btn-border-color: var(--app-color-primary);',
                '--bs-btn-hover-color: var(--app-color-on-strong);',
                '--bs-btn-hover-bg: var(--app-color-primary-hover);',
                '--bs-btn-hover-border-color: var(--app-color-primary-hover);',
                '--bs-btn-active-color: var(--app-color-on-strong);',
                '--bs-btn-active-bg: var(--app-color-primary-active);',
                '--bs-btn-active-border-color: var(--app-color-primary-active);',
            ),
        )
        self.assert_css_selector_group_contains(
            adapter_source,
            '.btn-primary',
            (
                '--bs-btn-disabled-color: var(--app-color-disabled-text);',
                '--bs-btn-disabled-bg: var(--app-color-disabled-bg);',
                '--bs-btn-disabled-border-color: var(--app-color-disabled-border);',
            ),
        )

    def test_warning_and_info_keep_fill_text_and_outline_roles_separate(self):
        adapter_source = self.read_frontend('styles/bootstrap-adapter.css')

        for color in ('warning', 'info'):
            with self.subTest(color=color, variant='filled'):
                self.assert_css_rule_contains(
                    adapter_source,
                    f'.btn-{color}',
                    (
                        '--bs-btn-color: var(--app-color-on-bright);',
                        f'--bs-btn-bg: var(--app-color-{color});',
                        f'--bs-btn-hover-bg: var(--app-color-{color}-hover);',
                        f'--bs-btn-active-bg: var(--app-color-{color}-active);',
                    ),
                )
            with self.subTest(color=color, variant='outline'):
                self.assert_css_rule_contains(
                    adapter_source,
                    f'.btn-outline-{color}',
                    (
                        f'--bs-btn-color: var(--app-color-{color}-text);',
                        f'--bs-btn-border-color: var(--app-color-{color}-text);',
                        '--bs-btn-hover-color: var(--app-color-on-bright);',
                        f'--bs-btn-hover-bg: var(--app-color-{color}-hover);',
                        '--bs-btn-active-color: var(--app-color-on-bright);',
                        f'--bs-btn-active-bg: var(--app-color-{color}-active);',
                    ),
                )

            for semantic_mapping in (
                f'--bs-{color}: var(--app-color-{color});',
                f'--bs-{color}-rgb: var(--app-color-{color}-rgb);',
                f'--bs-{color}-text-emphasis: var(--app-color-{color}-text);',
                f'--bs-{color}-bg-subtle: var(--app-color-{color}-soft);',
                f'--bs-{color}-border-subtle: var(--app-color-{color}-text);',
            ):
                with self.subTest(color=color, mapping=semantic_mapping):
                    self.assertIn(semantic_mapping, adapter_source)

            for link_state in ('hover', 'focus'):
                with self.subTest(color=color, link_state=link_state):
                    self.assert_css_selector_group_contains(
                        adapter_source,
                        f'.link-{color}:{link_state}',
                        (
                            f'color: var(--app-color-{color}-text) '
                            '!important;',
                        ),
                    )

    def test_controls_pagination_and_shell_use_context_tokens(self):
        adapter_source = self.read_frontend('styles/bootstrap-adapter.css')
        shell_source = self.read_frontend('styles/app-shell.css')

        self.assert_css_selector_group_contains(
            adapter_source,
            '.form-control',
            (
                'color: var(--app-color-text);',
                'background-color: var(--app-color-surface);',
                'border-color: var(--app-color-control-border);',
            ),
        )
        self.assert_css_selector_group_contains(
            adapter_source,
            '.form-control:focus',
            (
                'border-color: var(--app-color-focus-border);',
                'box-shadow: 0 0 0 var(--app-focus-ring-width) '
                'var(--app-focus-ring-color);',
            ),
        )
        self.assert_css_selector_group_contains(
            adapter_source,
            '.form-control:disabled',
            (
                'color: var(--app-color-disabled-text);',
                'background-color: var(--app-color-disabled-bg);',
                'border-color: var(--app-color-disabled-border);',
            ),
        )
        self.assert_css_rule_contains(
            adapter_source,
            '.pagination',
            (
                '--bs-pagination-border-color: var(--app-color-control-border);',
                '--bs-pagination-disabled-color: var(--app-color-disabled-text);',
                '--bs-pagination-disabled-bg: var(--app-color-disabled-bg);',
                '--bs-pagination-disabled-border-color: '
                'var(--app-color-disabled-border);',
            ),
        )

        self.assert_css_rule_contains(
            shell_source,
            '.app-navbar .navbar-toggler',
            ('border-color: var(--app-color-border);',),
        )
        self.assert_css_rule_contains(
            shell_source,
            '.app-navbar .navbar-toggler:focus',
            (
                'box-shadow: 0 0 0 var(--app-focus-ring-width) '
                'var(--app-focus-ring-color);',
            ),
        )
        self.assert_css_rule_contains(
            shell_source,
            '.app-navbar__search .form-control:focus',
            (
                'box-shadow: 0 0 0 var(--app-focus-ring-width) '
                'var(--app-focus-ring-color);',
            ),
        )
        self.assert_css_rule_contains(
            adapter_source,
            '.btn-close:focus',
            (
                'box-shadow: 0 0 0 var(--app-focus-ring-width) '
                'var(--app-focus-ring-color);',
            ),
        )

        self.assert_css_rule_contains(
            shell_source,
            '.app-navbar__notification-dot',
            ('background: var(--app-color-shell-notification);',),
        )

    def test_status_and_image_focus_states_use_contrast_safe_tokens(self):
        message_source = self.read_frontend('styles/site-message-page.css')
        components_source = self.read_frontend('styles/components.css')
        preview_source = self.read_frontend('styles/share-preview.css')

        self.assert_css_rule_contains(
            message_source,
            '.message-status--unread',
            ('border-color: var(--app-color-primary);',),
        )
        self.assert_css_rule_contains(
            components_source,
            '.spoiler-overlay .btn:focus-visible',
            (
                'outline: 2px solid var(--app-color-image-focus-inner);',
                'box-shadow: 0 0 0 0.375rem '
                'var(--app-color-image-focus-outer);',
            ),
        )
        self.assert_css_rule_contains(
            preview_source,
            '.share-preview__link',
            ('position: relative;',),
        )
        self.assert_css_rule_contains(
            preview_source,
            '.share-preview__link:focus-visible::after',
            (
                'z-index: calc(var(--app-z-content-overlay) + 1);',
                'border: 2px solid var(--app-color-image-focus-inner);',
                'box-shadow: 0 0 0 0.1875rem '
                'var(--app-color-image-focus-outer);',
            ),
        )

    def test_empty_copy_and_page_semantics_do_not_lower_contrast_with_opacity(self):
        cases = (
            (
                'styles/public-profile.css',
                '.public-profile-hero__empty-bio',
            ),
            (
                'styles/collection-page.css',
                '.collection-detail-hero__empty-description',
            ),
        )
        for stylesheet, selector in cases:
            with self.subTest(stylesheet=stylesheet, selector=selector):
                body = self.assert_css_rule_contains(
                    self.read_frontend(stylesheet),
                    selector,
                    ('font-style: italic;',),
                )
                self.assertNotIn('opacity:', body)

    def test_page_styles_do_not_embed_legacy_bootstrap_semantic_colors(self):
        legacy_colors = {
            'styles/share-detail-page.css': (
                '#084298',
                '#0f5132',
                '#664d03',
                '#842029',
                '#055160',
                'rgba(13, 110, 253,',
                'rgba(25, 135, 84,',
                'rgba(255, 193, 7,',
                'rgba(153, 115, 0,',
                'rgba(220, 53, 69,',
                'rgba(13, 202, 240,',
            ),
            'styles/share-editor-page.css': (
                '#8a6800',
                'rgba(255, 193, 7,',
                'rgba(153, 115, 0,',
                'rgba(220, 53, 69,',
            ),
            'styles/account-page.css': (
                'rgba(220, 53, 69,',
            ),
        }

        for stylesheet, forbidden_colors in legacy_colors.items():
            source = self.read_frontend(stylesheet).lower()
            for forbidden_color in forbidden_colors:
                with self.subTest(
                    stylesheet=stylesheet,
                    forbidden_color=forbidden_color,
                ):
                    self.assertNotIn(forbidden_color.lower(), source)

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

        based_content = render_to_string(
            'shares/includes/pagination.html',
            {
                'page_obj': page_obj,
                'aria_label': '带基址的测试分页',
                'base_url': '/staff/reviews/',
            },
            request=request,
        )
        self.assertIn(
            '/staff/reviews/?tab=review&amp;filter=open&amp;page=1',
            based_content,
        )
        self.assertIn(
            '/staff/reviews/?tab=review&amp;filter=open&amp;page=3',
            based_content,
        )

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

    def test_moderation_log_pages_use_shared_pagination_component(self):
        audit_source = self.read_template(
            'shares/includes/moderation_audit_log.html'
        )
        self.assertIn(
            "{% include 'shares/includes/pagination.html' with page_obj=logs",
            audit_source,
        )
        self.assertNotIn('logs.paginator.page_range', audit_source)
        for template_path in (
            'shares/admin_review_logs.html',
            'shares/admin_report_logs.html',
        ):
            with self.subTest(template=template_path):
                self.assertIn(
                    "shares/includes/moderation_audit_log.html",
                    self.read_template(template_path),
                )

        my_shares_source = self.read_template('shares/my_shares.html')
        self.assertIn(
            "{% include 'shares/includes/pagination.html' with page_obj=shares",
            my_shares_source,
        )
        self.assertNotIn('href="?page=', my_shares_source)

        public_profile_source = self.read_template('shares/user_public_profile.html')
        self.assertIn(
            "{% include 'shares/includes/pagination.html' with page_obj=shares",
            public_profile_source,
        )
        self.assertIn("aria_label='用户公开分享分页'", public_profile_source)
        self.assertNotIn('shares.paginator.page_range', public_profile_source)
        self.assertNotIn('href="?page=', public_profile_source)

    def test_public_profile_uses_semantic_server_navigation(self):
        source = self.read_template('shares/user_public_profile.html')
        main_styles = self.read_frontend('styles/main.css')
        profile_styles = self.read_frontend('styles/public-profile.css')

        self.assertIn('data-public-profile-page', source)
        self.assertIn(
            '<h1 class="ui-page-title public-profile-hero__name">',
            source,
        )
        self.assertIn('aria-label="用户主页内容"', source)
        self.assertIn("{% querystring tab='shares' page=None %}", source)
        self.assertIn("{% querystring tab='collections' page=None %}", source)
        self.assertIn('aria-current="page"', source)
        self.assertIn('data-public-profile-shares', source)
        self.assertIn('data-public-profile-collections', source)
        self.assertNotIn('data-bs-toggle="tab"', source)
        self.assertNotIn('role="tablist"', source)
        self.assertNotIn('role="tabpanel"', source)
        self.assertNotIn('style="', source)

        self.assertIn("@import './public-profile.css';", main_styles)
        self.assertIn('.public-profile-hero', profile_styles)
        self.assertIn('.public-profile-tabs', profile_styles)
        self.assertIn('@media (max-width: 575.98px)', profile_styles)

    def test_my_content_uses_semantic_server_navigation(self):
        source = self.read_template('shares/my_shares.html')
        main_styles = self.read_frontend('styles/main.css')
        page_styles = self.read_frontend('styles/my-content-page.css')

        self.assertIn('data-my-content-page', source)
        self.assertIn(
            '<h1 class="ui-page-title my-content-hero__title">',
            source,
        )
        self.assertIn('aria-label="我的内容分区"', source)
        self.assertIn("{% querystring tab='my_shares' page=None %}", source)
        for tab in ('collections', 'likes', 'favorites'):
            self.assertIn(
                f"{{% querystring tab='{tab}' page=None order=None %}}",
                source,
            )
        self.assertIn('aria-current="page"', source)
        self.assertIn("{% if current_tab != 'collections' %}", source)
        self.assertIn('data-my-content-shares', source)
        self.assertIn('data-my-content-collections', source)
        self.assertIn(
            "{% include 'shares/includes/pagination.html' with page_obj=shares",
            source,
        )
        self.assertIn(
            "{% include 'shares/includes/pagination.html' with page_obj=collections",
            source,
        )
        self.assertIn(
            "collection=collection card_variant='management' only",
            source,
        )
        self.assertIn("share=share card_variant='management' only", source)
        self.assertIn("share=share card_variant='browse' viewer=user only", source)
        self.assertIn("{% querystring order=None page=None %}", source)
        self.assertIn("{% querystring order='desc' page=None %}", source)
        self.assertIn("{% include 'shares/includes/empty_state.html' with ", source)
        self.assertNotIn('role="tablist"', source)
        self.assertNotIn('role="tabpanel"', source)
        self.assertNotIn('tab-pane', source)
        self.assertNotIn('data-bs-toggle', source)
        self.assertNotIn('style="', source)

        self.assertIn("@import './my-content-page.css';", main_styles)
        self.assertIn('.my-content-page {', page_styles)
        self.assertIn('.my-content-nav__list', page_styles)
        self.assertIn('@container my-content-page (max-width: 38rem)', page_styles)
        self.assertIn('@media (prefers-reduced-motion: reduce)', page_styles)

    def test_collection_card_variants_are_shared(self):
        public_profile_source = self.read_template('shares/user_public_profile.html')
        my_content_source = self.read_template('shares/my_shares.html')
        component_source = self.read_template('shares/includes/collection_card.html')
        card_styles = self.read_frontend('styles/collection-card.css')

        self.assertIn(
            "{% include 'shares/includes/collection_card.html' with "
            "collection=collection card_variant='public' only %}",
            public_profile_source,
        )
        self.assertIn(
            "{% include 'shares/includes/collection_card.html' with "
            "collection=collection card_variant='management' only %}",
            my_content_source,
        )
        self.assertIn('<article class="card h-100', component_source)
        self.assertIn('aria-labelledby="collection-card-title-', component_source)
        self.assertIn("{% if card_variant == 'public' %}", component_source)
        self.assertIn("{% if card_variant == 'management' %}", component_source)
        self.assertIn('data-public-collection', component_source)
        self.assertIn('data-managed-collection', component_source)
        self.assertIn("{% url 'collection_detail' collection.id %}", component_source)
        self.assertIn("{% url 'edit_collection' collection.id %}", component_source)
        self.assertIn("{% url 'delete_collection' collection.id %}", component_source)
        self.assertIn('<time datetime=', component_source)
        self.assertNotIn('style="', component_source)

        self.assertIn('.collection-card {', card_styles)
        self.assertIn('@container collection-card', card_styles)
        self.assertIn('@media (prefers-reduced-motion: reduce)', card_styles)

    def test_collection_detail_uses_semantic_paginated_components(self):
        source = self.read_template('shares/collection_detail.html')
        item_source = self.read_template('shares/includes/collection_item_card.html')
        main_styles = self.read_frontend('styles/main.css')
        collection_styles = self.read_frontend('styles/collection-page.css')

        self.assertIn('data-collection-detail-page', source)
        self.assertIn(
            '<h1 class="ui-page-title collection-detail-hero__title"',
            source,
        )
        self.assertIn('<ol class="row ', source)
        self.assertIn('data-collection-items', source)
        self.assertIn(
            "{% include 'shares/includes/collection_item_card.html' with ",
            source,
        )
        self.assertIn(
            "{% include 'shares/includes/pagination.html' with page_obj=items ",
            source,
        )
        self.assertIn('data-collection-manage-actions', source)
        self.assertIn('暂无可见内容', source)
        self.assertNotIn('style="', source)
        self.assertNotIn('collectionitem_set', source)

        self.assertIn('data-collection-item-card', item_source)
        self.assertEqual(
            item_source.count('?collection_id={{ collection.id }}'),
            3,
        )
        self.assertIn("method=\"post\"", item_source)
        self.assertIn('{% csrf_token %}', item_source)
        self.assertIn('data-confirm-message=', item_source)
        self.assertIn('data-remove-from-collection', item_source)
        self.assertIn('aria-label="从合集中移除《{{ item.share.title }}》"', item_source)
        self.assertNotIn('style="', item_source)

        self.assertIn("@import './collection-page.css';", main_styles)
        self.assertIn('.collection-detail-page', collection_styles)
        self.assertIn('.collection-item-card', collection_styles)
        self.assertIn('@container collection-item-card', collection_styles)
        self.assertIn('@media (max-width: 575.98px)', collection_styles)
        self.assertIn('@media (prefers-reduced-motion: reduce)', collection_styles)

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
            (
                'shares/includes/share_card.html',
                "share=share preview_variant='standard' "
                'spoiler_preference=spoiler_preference '
                'nsfw_preference=nsfw_preference',
            ),
            ('shares/includes/share_card.html', "share=share preview_variant='management'"),
            ('shares/includes/collection_item_card.html', "share=item.share preview_variant='standard'"),
            (
                'shares/includes/moderation_review_card.html',
                "share=share preview_variant='review'",
            ),
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

        collection_source = self.read_template('shares/includes/collection_item_card.html')
        review_source = self.read_template(
            'shares/includes/moderation_review_card.html'
        )
        self.assertIn('?collection_id={{ collection.id }}', collection_source)
        self.assertNotIn('share-preview__link', review_source)

        self.assertIn(
            'data-submit-on-change',
            self.read_template('shares/index.html'),
        )
        self.assertIn(
            'data-confirm-message',
            self.read_template('shares/includes/collection_item_card.html'),
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
            is_restricted=False,
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
        restricted_review = render_to_string(
            'shares/includes/share_preview.html',
            {
                'share': SimpleNamespace(
                    **{
                        **share.__dict__,
                        'status': 'approved',
                        'is_restricted': True,
                        'get_restriction_state_display': lambda: '举报下架限制',
                    },
                ),
                'preview_variant': 'review',
            },
        )

        self.assertIn('alt="预览 &amp; &quot;标题&quot; 的战术板预览"', standard)
        self.assertIn('/n/board/%5Bstgy%3Aa%26%22b%5D', standard)
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
        self.assertIn('举报下架限制', restricted_review)
        self.assertNotIn('待审核', restricted_review)

        explicitly_visible = render_to_string(
            'shares/includes/share_preview.html',
            {
                'share': share,
                'preview_variant': 'standard',
                'spoiler_preference': 'show',
                'nsfw_preference': 'show',
            },
        )
        spoiler_only_masked = render_to_string(
            'shares/includes/share_preview.html',
            {
                'share': share,
                'preview_variant': 'standard',
                'spoiler_preference': 'mask',
                'nsfw_preference': 'show',
            },
        )
        self.assertNotIn('blur-content', explicitly_visible)
        self.assertNotIn('share-preview__warning', explicitly_visible)
        self.assertIn('blur-content', spoiler_only_masked)
        self.assertIn('可能包含剧透', spoiler_only_masked)
        self.assertNotIn('可能令人不适', spoiler_only_masked)

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

    def test_reaction_buttons_submit_explicit_idempotent_target_state(self):
        form_source = self.read_template(
            'shares/includes/share_interaction_form.html',
        )
        self.assertIn('method="post"', form_source)
        self.assertIn('{% csrf_token %}', form_source)
        self.assertIn('type="hidden" name="next"', form_source)
        self.assertIn('share-interaction-form-{{ fragment }}-', form_source)

        for template_path in (
            'shares/includes/like_button.html',
            'shares/includes/favorite_button.html',
        ):
            with self.subTest(template=template_path):
                source = self.read_template(template_path)
                self.assertIn('hx-vals=', source)
                self.assertIn('"target_state":', source)
                self.assertIn('active', source)
                self.assertIn('inactive', source)
                self.assertIn('type="submit"', source)
                self.assertIn('form="share-interaction-form-', source)
                self.assertIn('formaction="{% url ', source)
                self.assertIn('name="target_state"', source)
                self.assertIn('value="{% if ', source)
                self.assertIn('hx-sync="this:drop"', source)
                self.assertIn('hx-disabled-elt="this"', source)
                self.assertIn('data-share-interaction=', source)
                self.assertNotIn('hx-on', source)

    def test_home_template_delegates_announcement_and_infinite_scroll(self):
        source = self.read_template('shares/index.html')
        page_source = self.read_template('shares/includes/share_cards_page.html')
        announcement_source = self.read_frontend('features/announcement.ts')

        self.assertIn('data-dismiss-announcement', source)
        self.assertIn("shares/includes/share_cards_page.html", source)
        self.assertNotIn('<script', source)
        self.assertNotIn('dismissAnnouncement()', source)
        self.assertNotIn('initInfiniteScroll', source)
        self.assertNotIn('insertAdjacentHTML', source)
        self.assertIn('data-infinite-scroll-sentinel', page_source)
        self.assertIn('hx-trigger="intersect, click"', page_source)
        self.assertNotIn('hx-trigger="revealed"', page_source)
        self.assertIn("clone.removeAttribute('id')", announcement_source)
        self.assertIn("clone.removeAttribute('aria-labelledby')", announcement_source)
        self.assertIn("clone.setAttribute('aria-hidden', 'true')", announcement_source)

    def test_home_uses_scoped_responsive_browse_layout_contract(self):
        source = self.read_template('shares/index.html')
        main_styles = self.read_frontend('styles/main.css')
        browse_styles = self.read_frontend('styles/browse-page.css')

        for hook in (
            'data-browse-page',
            'data-browse-toolbar',
            'data-browse-results',
        ):
            self.assertIn(hook, source)

        self.assertIn('aria-labelledby="browse-toolbar-title"', source)
        self.assertIn('aria-labelledby="browse-results-title"', source)
        self.assertIn('aria-label="敏感内容显示方式"', source)
        self.assertIn('aria-label="剧透内容显示方式"', source)
        self.assertIn('aria-label="令人不适内容显示方式"', source)
        self.assertIn('<span>高级</span>', source)
        self.assertIn('id="browse-advanced-title">高级浏览</h3>', source)
        self.assertNotIn('<span>筛选</span>', source)
        for field in ('spoiler', 'nsfw'):
            for value in ('hide', 'mask', 'show'):
                self.assertIn(
                    f'name="{field}" id="{field}-{value}" value="{value}"',
                    source,
                )
        self.assertIn('aria-labelledby="browse-mode-label"', source)
        self.assertIn('aria-pressed="{% if feed_mode ==', source)
        self.assertIn("@import './browse-page.css';", main_styles)
        self.assertIn('.browse-toolbar__controls', browse_styles)
        self.assertIn('.browse-filter-panel__header', browse_styles)
        self.assertIn('.browse-advanced-secondary', browse_styles)
        self.assertIn('min-width: 0;', browse_styles)
        self.assertIn('@media (max-width: 575.98px)', browse_styles)

    def test_detail_basic_interactions_use_module_contract(self):
        source = self.read_template('shares/detail.html')
        collections_source = self.read_template(
            'shares/includes/share_detail_collections.html',
        )
        modals_source = self.read_template('shares/includes/share_detail_modals.html')
        combined_source = source + collections_source + modals_source
        module_source = self.read_frontend('features/share-detail.ts')
        copy_source = self.read_frontend('features/share-copy.ts')

        for hook in (
            'data-share-detail',
            'data-content-overlay',
            'data-copy-detail-code',
            'data-copy-share-url',
            'data-views-count',
            'data-copies-count',
        ):
            self.assertIn(hook, combined_source)

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
        self.assertIn('const canonicalShareUrl = root.dataset.shareUrl', module_source)
        self.assertIn('const url = root.dataset.shareUrl', module_source)
        self.assertNotIn('window.location.pathname', module_source)
        self.assertIn('data-share-url="{{ canonical_share_url }}"', source)
        self.assertIn('value="{{ canonical_share_url }}"', source)
        self.assertIn('data-share-url-input', source)
        self.assertEqual(collections_source.count('{{ item.visible_position }}'), 2)
        self.assertIn('updateViewsCounter(root, payload)', module_source)
        self.assertIn('performShareCopy({', module_source)
        self.assertIn('recordUrl: root.dataset.recordCopyUrl', module_source)
        self.assertIn("querySelectorAll<HTMLElement>('[data-copies-count]')", copy_source)

    def test_detail_uses_semantic_responsive_page_contract(self):
        source = self.read_template('shares/detail.html')
        collections_source = self.read_template(
            'shares/includes/share_detail_collections.html',
        )
        modals_source = self.read_template('shares/includes/share_detail_modals.html')
        combined_source = source + collections_source + modals_source
        main_styles = self.read_frontend('styles/main.css')
        detail_styles = self.read_frontend('styles/share-detail-page.css')

        self.assertIn('<article', source)
        self.assertIn('class="share-detail-page"', source)
        self.assertIn('aria-labelledby="share-detail-title"', source)
        self.assertEqual(source.count('<h1'), 1)
        self.assertLess(
            source.index('class="ui-page-header share-detail-hero"'),
            source.index(
                'class="ui-panel share-detail-panel share-detail-preview"',
            ),
        )

        for hook in (
            'data-content-revealed=',
            'data-content-overlay',
            'data-reveal-content',
            'data-share-image-warning',
        ):
            self.assertIn(hook, source)
        self.assertIn('src="{{ detail.preview_url }}"', source)
        self.assertIn('for="share-detail-code"', source)
        self.assertIn('for="share-detail-url"', source)
        self.assertIn('aria-describedby="share-image-warning-help" disabled', source)

        for forbidden in (
            'style=',
            'class="row',
            'col-lg-',
            'nav-tabs',
            'tab-pane',
            'data-bs-toggle="tab"',
            'data-bs-toggle="collapse"',
            'share.status',
            'share.visibility',
            'share.category',
            'share.is_spoiler',
            'share.is_nsfw',
            'user.is_authenticated',
            'user ==',
        ):
            self.assertNotIn(forbidden, combined_source)

        self.assertIn('<details', collections_source)
        self.assertIn('selected_collection_id == collection.id', collections_source)
        self.assertIn('aria-current="page"', collections_source)
        self.assertIn('aria-labelledby="share-image-modal-title"', modals_source)
        self.assertIn('aria-label="关闭分享图片预览"', modals_source)
        self.assertIn('role="img"', modals_source)
        self.assertIn('当前浏览器无法显示生成的分享图片预览。', modals_source)

        self.assertIn("@import './share-detail-page.css';", main_styles)
        for contract in (
            'container-name: share-detail-page;',
            'grid-template-columns:',
            'min-width: 0;',
            ".share-detail-page[data-content-revealed='true'] [data-share-image-warning]",
            '@container share-detail-page',
            '@media (max-width: 575.98px)',
            '@media (prefers-reduced-motion: reduce)',
            ':focus-visible',
        ):
            self.assertIn(contract, detail_styles)

    def test_share_image_uses_module_contract_and_only_qrcode_runtime(self):
        source = self.read_template('shares/detail.html')
        modals_source = self.read_template('shares/includes/share_detail_modals.html')
        combined_source = source + modals_source
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
            self.assertIn(hook, combined_source)

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
        self.assertIn('const cleanUrl = elements.root.dataset.shareUrl', module_source)
        self.assertNotIn('window.location.pathname', module_source)
        self.assertIn('const targetWidth = 960', module_source)
        self.assertIn('const targetHeight = 720', module_source)
        self.assertIn("if (blob === null)", module_source)
        self.assertIn("typeof ClipboardItem !== 'undefined'", module_source)
        self.assertIn('getBootstrapModal(modalElement)', module_source)
        self.assertIn("root.dataset.contentRevealed === 'true'", module_source)
        self.assertIn("root.addEventListener('share:content-revealed'", module_source)
        self.assertIn('fitCanvasText(context, title', module_source)
        self.assertIn('container.remove()', module_source)
        self.assertIn('link?.remove()', module_source)
