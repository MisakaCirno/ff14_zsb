from datetime import timedelta
from html.parser import HTMLParser
from importlib import import_module
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.apps import apps as django_apps
from django.conf import settings
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import AnonymousUser, User
from django.db import connection
from django.test import Client, RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .admin import SiteMessageAdmin
from .models import Share, SiteMessage
from .selectors import unread_site_message_count
from .services.messages import send_site_message


_VOID_ELEMENTS = {
    'area',
    'base',
    'br',
    'col',
    'embed',
    'hr',
    'img',
    'input',
    'link',
    'meta',
    'param',
    'source',
    'track',
    'wbr',
}


class _PageProbe(HTMLParser):
    """Small DOM-like probe for structural accessibility contracts."""

    def __init__(self):
        super().__init__()
        self.elements = []
        self._open_elements = []

    def handle_starttag(self, tag, attrs):
        element = {
            'uid': len(self.elements),
            'tag': tag,
            'attrs': dict(attrs),
            'ancestors': tuple(item['uid'] for item in self._open_elements),
        }
        self.elements.append(element)
        if tag not in _VOID_ELEMENTS:
            self._open_elements.append(element)

    def handle_startendtag(self, tag, attrs):
        self.elements.append({
            'uid': len(self.elements),
            'tag': tag,
            'attrs': dict(attrs),
            'ancestors': tuple(item['uid'] for item in self._open_elements),
        })

    def handle_endtag(self, tag):
        for index in range(len(self._open_elements) - 1, -1, -1):
            if self._open_elements[index]['tag'] == tag:
                del self._open_elements[index:]
                return

    def matching(self, *, tag=None, attribute=None, value=None):
        return [
            element
            for element in self.elements
            if (tag is None or element['tag'] == tag)
            and (attribute is None or attribute in element['attrs'])
            and (
                attribute is None
                or value is None
                or element['attrs'].get(attribute) == value
            )
        ]

    def descendants(self, element, *, tag=None):
        return [
            candidate
            for candidate in self.elements
            if element['uid'] in candidate['ancestors']
            and (tag is None or candidate['tag'] == tag)
        ]


class SiteMessageFixtureMixin:
    password = 'CurrentPassword123!'

    def create_message(
        self,
        *,
        recipient,
        title='站内信契约',
        content='站内信正文',
        sender=None,
        read_at=None,
        archived_at=None,
        related_share=None,
        metadata=None,
        created_at=None,
    ):
        message = SiteMessage.objects.create(
            recipient=recipient,
            sender=sender,
            message_type=SiteMessage.MessageType.REPORT_RESOLVED,
            title=title,
            content=content,
            read_at=read_at,
            archived_at=archived_at,
            related_share=related_share,
            metadata=metadata or {},
        )
        if created_at is not None:
            SiteMessage.objects.filter(pk=message.pk).update(created_at=created_at)
            message.created_at = created_at
        return message

    def archive_url(self, message):
        return reverse('set_site_message_archive_state', args=[message.pk])

    def assert_private_no_store(self, response):
        cache_control = response.headers.get('Cache-Control', '')
        directives = {
            item.strip().lower()
            for item in cache_control.split(',')
            if item.strip()
        }
        self.assertIn('private', directives)
        self.assertIn('no-store', directives)
        self.assertIn('Cookie', response.headers.get('Vary', ''))

    def page_probe(self, response):
        probe = _PageProbe()
        probe.feed(response.content.decode(response.charset))
        return probe


class SiteMessageHttpContractTests(SiteMessageFixtureMixin, TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='message-owner',
            password=self.password,
        )
        self.other = User.objects.create_user(
            username='message-other',
            password=self.password,
        )
        self.staff = User.objects.create_user(
            username='message-staff',
            password=self.password,
            is_staff=True,
        )
        self.message = self.create_message(recipient=self.owner)

    def test_endpoints_require_authentication_and_enforce_exact_methods(self):
        detail_url = reverse('site_message_detail', args=[self.message.pk])
        write_urls = (
            reverse('open_site_message', args=[self.message.pk]),
            reverse('mark_all_site_messages_read'),
            self.archive_url(self.message),
        )

        anonymous_cases = (
            ('get', reverse('site_message_list')),
            ('get', detail_url),
            *((('post', url) for url in write_urls)),
        )
        for method, url in anonymous_cases:
            with self.subTest(authentication=url):
                response = getattr(self.client, method)(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.url.startswith(reverse('login')))

        self.client.force_login(self.owner)
        for url in (reverse('site_message_list'), detail_url):
            with self.subTest(safe_endpoint=url):
                self.assertEqual(self.client.get(url).status_code, 200)
                self.assertEqual(self.client.head(url).status_code, 200)
                response = self.client.post(url)
                self.assertEqual(response.status_code, 405)
                self.assertEqual(response.headers['Allow'], 'GET, HEAD')

        for url in write_urls:
            with self.subTest(write_endpoint=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 405)
                self.assertEqual(response.headers['Allow'], 'POST')

        self.message.refresh_from_db()
        self.assertIsNone(self.message.read_at)
        self.assertIsNone(self.message.archived_at)

    def test_authenticated_responses_are_private_and_never_cached(self):
        self.client.force_login(self.owner)
        read_message = self.create_message(
            recipient=self.owner,
            read_at=timezone.now(),
        )
        archived_message = self.create_message(
            recipient=self.owner,
            archived_at=timezone.now(),
        )
        cases = (
            self.client.get(reverse('site_message_list')),
            self.client.get(
                reverse('site_message_detail', args=[self.message.pk]),
            ),
            self.client.post(
                reverse('open_site_message', args=[read_message.pk]),
            ),
            self.client.post(reverse('mark_all_site_messages_read')),
            self.client.post(
                self.archive_url(archived_message),
                {'target_state': 'inbox'},
            ),
        )

        for response in cases:
            with self.subTest(path=response.request['PATH_INFO']):
                self.assert_private_no_store(response)

    def test_all_state_changes_require_csrf_and_leave_data_unchanged_on_failure(self):
        archived_message = self.create_message(
            recipient=self.owner,
            archived_at=timezone.now(),
        )
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        cases = (
            (
                reverse('open_site_message', args=[self.message.pk]),
                {},
            ),
            (reverse('mark_all_site_messages_read'), {}),
            (
                self.archive_url(archived_message),
                {'target_state': 'inbox'},
            ),
        )

        for url, payload in cases:
            with self.subTest(url=url):
                self.assertEqual(csrf_client.post(url, payload).status_code, 403)

        self.message.refresh_from_db()
        archived_message.refresh_from_db()
        self.assertIsNone(self.message.read_at)
        self.assertIsNone(self.message.archived_at)
        self.assertIsNone(archived_message.read_at)
        self.assertIsNotNone(archived_message.archived_at)

    def test_owner_isolation_applies_to_list_detail_open_and_archive(self):
        archived = self.create_message(
            recipient=self.owner,
            title='所有者归档消息',
            archived_at=timezone.now(),
        )
        foreign = self.create_message(
            recipient=self.other,
            title='其他用户私有消息',
        )
        self.client.force_login(self.owner)

        inbox = self.client.get(reverse('site_message_list'))
        self.assertEqual(inbox.status_code, 200)
        self.assertContains(inbox, self.message.title)
        self.assertNotContains(inbox, archived.title)
        self.assertNotContains(inbox, foreign.title)
        self.assertEqual(
            self.client.get(
                reverse('site_message_detail', args=[archived.pk]),
            ).status_code,
            200,
        )

        for actor in (self.owner, self.staff):
            self.client.force_login(actor)
            with self.subTest(actor=actor.username, action='detail'):
                self.assertEqual(
                    self.client.get(
                        reverse('site_message_detail', args=[foreign.pk]),
                    ).status_code,
                    404,
                )
            with self.subTest(actor=actor.username, action='open'):
                self.assertEqual(
                    self.client.post(
                        reverse('open_site_message', args=[foreign.pk]),
                    ).status_code,
                    404,
                )
            with self.subTest(actor=actor.username, action='archive'):
                self.assertEqual(
                    self.client.post(
                        self.archive_url(foreign),
                        {'target_state': 'archived'},
                    ).status_code,
                    404,
                )

        foreign.refresh_from_db()
        self.assertIsNone(foreign.read_at)
        self.assertIsNone(foreign.archived_at)

    def test_open_marks_only_the_first_read_time_and_is_idempotent(self):
        self.client.force_login(self.owner)
        first_read_at = timezone.now()
        later_attempt = first_read_at + timedelta(minutes=5)
        url = reverse('open_site_message', args=[self.message.pk])

        with patch('shares.services.messages.timezone.now', return_value=first_read_at):
            first = self.client.post(url)
        self.assertRedirects(
            first,
            reverse('site_message_detail', args=[self.message.pk]),
        )
        self.message.refresh_from_db()
        self.assertEqual(self.message.read_at, first_read_at)

        with patch('shares.services.messages.timezone.now', return_value=later_attempt):
            second = self.client.post(url)
        self.assertRedirects(
            second,
            reverse('site_message_detail', args=[self.message.pk]),
        )
        self.message.refresh_from_db()
        self.assertEqual(self.message.read_at, first_read_at)

    def test_archive_state_is_explicit_owner_scoped_and_idempotent(self):
        self.client.force_login(self.owner)
        archived_at = timezone.now()
        later_attempt = archived_at + timedelta(minutes=5)
        url = self.archive_url(self.message)

        with patch('shares.services.messages.timezone.now', return_value=archived_at):
            response = self.client.post(url, {
                'target_state': 'archived',
                'mailbox': 'unread',
                'page': '2',
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            f"{reverse('site_message_list')}?mailbox=unread&page=2",
        )
        self.message.refresh_from_db()
        self.assertEqual(self.message.archived_at, archived_at)
        self.assertEqual(unread_site_message_count(self.owner), 0)

        with patch('shares.services.messages.timezone.now', return_value=later_attempt):
            response = self.client.post(url, {'target_state': 'archived'})
        self.assertEqual(response.status_code, 302)
        self.message.refresh_from_db()
        self.assertEqual(self.message.archived_at, archived_at)

        for payload in ({}, {'target_state': 'unknown'}):
            with self.subTest(invalid_target=payload):
                invalid = self.client.post(url, payload)
                self.assertEqual(invalid.status_code, 400)
                self.message.refresh_from_db()
                self.assertEqual(self.message.archived_at, archived_at)

        restored = self.client.post(url, {
            'target_state': 'inbox',
            'mailbox': 'archived',
            'page': '3',
        })
        self.assertEqual(restored.status_code, 302)
        self.assertEqual(
            restored.url,
            f"{reverse('site_message_list')}?mailbox=archived&page=3",
        )
        self.message.refresh_from_db()
        self.assertIsNone(self.message.archived_at)
        self.assertEqual(unread_site_message_count(self.owner), 1)

    def test_mark_all_read_updates_only_active_unread_messages(self):
        existing_read_at = timezone.now() - timedelta(days=1)
        batch_read_at = timezone.now()
        already_read = self.create_message(
            recipient=self.owner,
            title='已读消息',
            read_at=existing_read_at,
        )
        archived = self.create_message(
            recipient=self.owner,
            title='归档未读消息',
            archived_at=timezone.now(),
        )
        foreign = self.create_message(
            recipient=self.other,
            title='他人未读消息',
        )
        self.client.force_login(self.owner)

        with patch('shares.services.messages.timezone.now', return_value=batch_read_at):
            response = self.client.post(
                reverse('mark_all_site_messages_read'),
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '已将 1 条站内信标记为已读')

        for message in (self.message, already_read, archived, foreign):
            message.refresh_from_db()
        self.assertEqual(self.message.read_at, batch_read_at)
        self.assertEqual(already_read.read_at, existing_read_at)
        self.assertIsNone(archived.read_at)
        self.assertIsNone(foreign.read_at)

        repeated = self.client.post(
            reverse('mark_all_site_messages_read'),
            follow=True,
        )
        self.assertContains(repeated, '已将 0 条站内信标记为已读')
        self.message.refresh_from_db()
        self.assertEqual(self.message.read_at, batch_read_at)


class SiteMessageMailboxContractTests(SiteMessageFixtureMixin, TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='mailbox-owner',
            password=self.password,
        )
        self.other = User.objects.create_user(
            username='mailbox-other',
            password=self.password,
        )
        self.client.force_login(self.owner)

    def page_ids(self, response):
        return [message.pk for message in response.context['site_messages']]

    def assert_current_mailbox(self, response, expected):
        probe = self.page_probe(response)
        mailbox_navs = []
        for nav in probe.matching(tag='nav'):
            links = [
                link
                for link in probe.descendants(nav, tag='a')
                if 'mailbox' in parse_qs(
                    urlsplit(link['attrs'].get('href', '')).query,
                )
            ]
            if links:
                mailbox_navs.append((nav, links))

        self.assertEqual(len(mailbox_navs), 1)
        _, links = mailbox_navs[0]
        self.assertEqual(
            {
                parse_qs(urlsplit(link['attrs']['href']).query)['mailbox'][0]
                for link in links
            },
            {'inbox', 'unread', 'archived'},
        )
        current = [
            link
            for link in links
            if link['attrs'].get('aria-current') == 'page'
        ]
        self.assertEqual(len(current), 1)
        self.assertEqual(
            parse_qs(urlsplit(current[0]['attrs']['href']).query)['mailbox'],
            [expected],
        )

    def assert_archive_control(
        self,
        response,
        message,
        target_state,
        mailbox,
    ):
        probe = self.page_probe(response)
        forms = [
            form
            for form in probe.matching(tag='form')
            if form['attrs'].get('method', '').lower() == 'post'
            and form['attrs'].get('action') == self.archive_url(message)
        ]
        self.assertEqual(len(forms), 1)
        inputs = probe.descendants(forms[0], tag='input')
        self.assertTrue(any(
            item['attrs'].get('name') == 'csrfmiddlewaretoken'
            for item in inputs
        ))
        self.assertTrue(any(
            item['attrs'].get('name') == 'target_state'
            and item['attrs'].get('value') == target_state
            for item in inputs
        ))
        self.assertTrue(any(
            item['attrs'].get('name') == 'mailbox'
            and item['attrs'].get('value') == mailbox
            for item in inputs
        ))
        self.assertTrue(any(
            item['attrs'].get('name') == 'page'
            and item['attrs'].get('value') == '1'
            for item in inputs
        ))

    def test_mailbox_partitions_and_invalid_value_falls_back_to_inbox(self):
        unread = self.create_message(recipient=self.owner, title='收件箱未读')
        read = self.create_message(
            recipient=self.owner,
            title='收件箱已读',
            read_at=timezone.now(),
        )
        archived_unread = self.create_message(
            recipient=self.owner,
            title='归档未读',
            archived_at=timezone.now(),
        )
        archived_read = self.create_message(
            recipient=self.owner,
            title='归档已读',
            read_at=timezone.now(),
            archived_at=timezone.now(),
        )
        self.create_message(recipient=self.other, title='其他用户消息')

        cases = (
            ('inbox', [read.pk, unread.pk], unread, 'archived'),
            ('unread', [unread.pk], unread, 'archived'),
            (
                'archived',
                [archived_read.pk, archived_unread.pk],
                archived_unread,
                'inbox',
            ),
        )
        for mailbox, expected_ids, controlled_message, target_state in cases:
            with self.subTest(mailbox=mailbox):
                response = self.client.get(
                    reverse('site_message_list'),
                    {'mailbox': mailbox},
                )
                self.assertEqual(response.status_code, 200)
                self.assertCountEqual(self.page_ids(response), expected_ids)
                self.assert_current_mailbox(response, mailbox)
                self.assert_archive_control(
                    response,
                    controlled_message,
                    target_state,
                    mailbox,
                )

        fallback = self.client.get(
            reverse('site_message_list'),
            {'mailbox': 'unsupported'},
        )
        self.assertEqual(fallback.status_code, 200)
        self.assertCountEqual(self.page_ids(fallback), [read.pk, unread.pk])
        self.assert_current_mailbox(fallback, 'inbox')

    def test_unread_count_excludes_read_archived_and_foreign_messages(self):
        self.create_message(recipient=self.owner)
        self.create_message(recipient=self.owner, read_at=timezone.now())
        self.create_message(recipient=self.owner, archived_at=timezone.now())
        self.create_message(recipient=self.other)

        self.assertEqual(unread_site_message_count(AnonymousUser()), 0)
        self.assertEqual(unread_site_message_count(self.owner), 1)
        self.assertEqual(unread_site_message_count(self.other), 1)

    def test_pagination_is_stable_and_preserves_mailbox(self):
        created_at = timezone.now()
        messages = [
            self.create_message(
                recipient=self.owner,
                title=f'分页消息 {index:02d}',
                created_at=created_at,
            )
            for index in range(22)
        ]
        expected_ids = sorted(
            (message.pk for message in messages),
            reverse=True,
        )

        first = self.client.get(
            reverse('site_message_list'),
            {'mailbox': 'inbox', 'page': 1},
        )
        second = self.client.get(
            reverse('site_message_list'),
            {'mailbox': 'inbox', 'page': 2},
        )

        self.assertEqual(first.context['site_messages'].paginator.count, 22)
        self.assertEqual(self.page_ids(first), expected_ids[:20])
        self.assertEqual(self.page_ids(second), expected_ids[20:])
        self.assertContains(first, '?mailbox=inbox&amp;page=2')
        self.assertContains(first, 'aria-current="page"')
        self.assertContains(first, 'aria-disabled="true"')
        self.assertContains(first, 'rel="next"')
        self.assertContains(second, 'rel="prev"')

    def test_list_and_detail_query_budgets_do_not_scale_per_message(self):
        sender = User.objects.create_user(
            username='mailbox-sender',
            password=self.password,
        )
        share = Share.objects.create(
            title='查询预算关联分享',
            strategy_code='[stgy:message-query-budget]',
            author=sender,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )
        messages = [
            self.create_message(
                recipient=self.owner,
                sender=sender,
                related_share=share,
                title=f'查询预算消息 {index:02d}',
            )
            for index in range(20)
        ]

        with CaptureQueriesContext(connection) as list_queries:
            response = self.client.get(
                reverse('site_message_list'),
                {'mailbox': 'inbox'},
            )
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(list_queries), 6)

        with CaptureQueriesContext(connection) as detail_queries:
            response = self.client.get(
                reverse('site_message_detail', args=[messages[0].pk]),
            )
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(detail_queries), 5)


class SiteMessageUiContractTests(SiteMessageFixtureMixin, TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='message-ui-owner',
            password=self.password,
        )
        self.author = User.objects.create_user(
            username='message-ui-author',
            password=self.password,
        )
        self.client.force_login(self.owner)

    def test_related_share_link_requires_view_permission_and_text_is_escaped(self):
        visible_share = Share.objects.create(
            title='可访问关联分享',
            strategy_code='[stgy:visible-message-link]',
            author=self.author,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )
        private_share = Share.objects.create(
            title='不可访问关联分享',
            strategy_code='[stgy:private-message-link]',
            author=self.author,
            visibility=Share.Visibility.PRIVATE,
            status=Share.Status.APPROVED,
        )
        malicious = self.create_message(
            recipient=self.owner,
            sender=self.author,
            title='<script>alert("title")</script>',
            content='<img src=x onerror="alert(1)">\njavascript:alert(2)',
            related_share=visible_share,
            metadata={'url': 'javascript:alert(3)'},
        )
        inaccessible = self.create_message(
            recipient=self.owner,
            title='不可访问关联消息',
            related_share=private_share,
        )

        response = self.client.get(
            reverse('site_message_detail', args=[malicious.pk]),
        )
        markup = response.content.decode(response.charset)
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            reverse('share_detail', args=[visible_share.share_id]),
        )
        self.assertNotIn('<script>alert("title")</script>', markup)
        self.assertNotIn('<img src=x onerror=', markup)
        self.assertNotIn('javascript:alert(3)', markup)
        self.assertContains(
            response,
            '&lt;script&gt;alert(&quot;title&quot;)&lt;/script&gt;',
            html=False,
        )

        response = self.client.get(
            reverse('site_message_detail', args=[inaccessible.pk]),
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(
            response,
            reverse('share_detail', args=[private_share.share_id]),
        )

    def test_unread_detail_has_explicit_post_mark_read_form(self):
        message = self.create_message(recipient=self.owner)
        response = self.client.get(
            reverse('site_message_detail', args=[message.pk]),
        )
        probe = self.page_probe(response)
        expected_action = reverse('open_site_message', args=[message.pk])
        forms = [
            form
            for form in probe.matching(tag='form')
            if form['attrs'].get('method', '').lower() == 'post'
            and form['attrs'].get('action') == expected_action
        ]

        self.assertEqual(len(forms), 1)
        csrf_inputs = [
            element
            for element in probe.descendants(forms[0], tag='input')
            if element['attrs'].get('name') == 'csrfmiddlewaretoken'
        ]
        self.assertEqual(len(csrf_inputs), 1)

        message.read_at = timezone.now()
        message.save(update_fields=['read_at'])
        read_response = self.client.get(
            reverse('site_message_detail', args=[message.pk]),
        )
        read_probe = self.page_probe(read_response)
        read_forms = [
            form
            for form in read_probe.matching(tag='form')
            if form['attrs'].get('method', '').lower() == 'post'
            and form['attrs'].get('action') == expected_action
        ]
        self.assertEqual(read_forms, [])

    def test_list_and_detail_use_accessible_semantics_and_shared_components(self):
        message = self.create_message(recipient=self.owner)
        list_response = self.client.get(
            reverse('site_message_list'),
            {'mailbox': 'inbox'},
        )
        detail_response = self.client.get(
            reverse('site_message_detail', args=[message.pk]),
        )

        for response, expected_container in (
            (list_response, 'ol'),
            (detail_response, 'article'),
        ):
            with self.subTest(path=response.request['PATH_INFO']):
                probe = self.page_probe(response)
                main = probe.matching(tag='main', attribute='id', value='main-content')
                self.assertEqual(len(main), 1)
                self.assertEqual(len(probe.descendants(main[0], tag='h1')), 1)
                self.assertTrue(probe.descendants(main[0], tag=expected_container))
                times = probe.descendants(main[0], tag='time')
                self.assertTrue(times)
                self.assertTrue(all(item['attrs'].get('datetime') for item in times))
                self.assertFalse([
                    element
                    for element in probe.descendants(main[0])
                    if 'style' in element['attrs']
                ])
                for icon in probe.descendants(main[0], tag='i'):
                    self.assertEqual(icon['attrs'].get('aria-hidden'), 'true')

        self.assertContains(list_response, '未读')
        self.assertContains(list_response, '标记为已读并查看：')

        list_probe = self.page_probe(list_response)
        message_lists = list_probe.matching(
            tag='ol',
            attribute='data-site-message-list',
        )
        self.assertEqual(len(message_lists), 1)
        list_items = list_probe.descendants(message_lists[0], tag='li')
        articles = list_probe.descendants(message_lists[0], tag='article')
        self.assertEqual(len(list_items), 1)
        self.assertEqual(len(articles), 1)
        self.assertIn(list_items[0]['uid'], articles[0]['ancestors'])

        template_root = Path(settings.BASE_DIR) / 'templates'
        list_source = (
            template_root / 'shares' / 'site_message_list.html'
        ).read_text(encoding='utf-8')
        base_source = (template_root / 'base.html').read_text(encoding='utf-8')
        self.assertIn("shares/includes/pagination.html", list_source)
        self.assertIn("shares/includes/empty_state.html", list_source)

        base_probe = _PageProbe()
        base_probe.feed(base_source)
        noscript = base_probe.matching(tag='noscript')
        self.assertEqual(len(noscript), 1)
        message_links = [
            link
            for link in base_probe.descendants(noscript[0], tag='a')
            if "{% url 'site_message_list' %}" in link['attrs'].get('href', '')
        ]
        self.assertEqual(len(message_links), 1)


class SiteMessageIntegrityContractTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='message-integrity-user',
            password='password123',
            is_superuser=True,
            is_staff=True,
        )

    def test_shared_sender_bounds_new_titles_without_losing_body_content(self):
        full_title = '超长通知标题' * 60
        body = f'完整正文保留标题：{full_title}'

        message = send_site_message(
            recipient=self.user,
            message_type=SiteMessage.MessageType.REPORT_RESOLVED,
            title=full_title,
            content=body,
        )

        max_length = SiteMessage._meta.get_field('title').max_length
        self.assertEqual(max_length, 255)
        self.assertLessEqual(len(message.title), max_length)
        self.assertEqual(message.content, body)
        self.assertIn(full_title, message.content)

    def test_site_message_admin_is_read_only_and_cannot_delete_user_data(self):
        model_admin = SiteMessageAdmin(SiteMessage, AdminSite())
        request = RequestFactory().get('/admin/shares/sitemessage/')
        request.user = self.user

        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request))
        self.assertIsNone(model_admin.actions)
        self.assertEqual(
            set(model_admin.readonly_fields),
            {
                field.name
                for field in SiteMessage._meta.fields
                if not field.primary_key
            },
        )

    def test_reverse_migration_refuses_to_narrow_overlong_titles(self):
        SiteMessage.objects.create(
            recipient=self.user,
            message_type=SiteMessage.MessageType.REPORT_RESOLVED,
            title='x' * 201,
            content='The title must survive a rollback attempt.',
        )
        migration = import_module(
            'shares.migrations.0024_widen_site_message_titles',
        )

        class SchemaEditorStub:
            connection = connection

        with self.assertRaisesRegex(RuntimeError, 'longer than 200'):
            migration.ensure_site_message_titles_fit_legacy(
                django_apps,
                SchemaEditorStub(),
            )

        reverse_guard = migration.Migration.operations[-1]
        self.assertIs(
            reverse_guard.reverse_code,
            migration.ensure_site_message_titles_fit_legacy,
        )
