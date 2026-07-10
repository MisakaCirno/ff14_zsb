from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from .models import Announcement, Collection, CollectionItem, Report, Share, SiteMessage


class SafeMethodContractTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username='admin',
            password='password123',
            is_staff=True,
        )
        self.share = Share.objects.create(
            title='待审核分享',
            strategy_code='[stgy:http-safety]',
            author=self.admin,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.PENDING,
        )
        self.announcement = Announcement.objects.create(title='动态', content='内容')
        self.report = Report.objects.create(
            share=self.share,
            reporter=self.admin,
            reason='测试举报',
        )
        self.message = SiteMessage.objects.create(
            recipient=self.admin,
            message_type=SiteMessage.MessageType.REPORT_RESOLVED,
            title='测试消息',
            content='内容',
        )
        self.collection = Collection.objects.create(title='合集', author=self.admin)
        CollectionItem.objects.create(collection=self.collection, share=self.share)
        self.client.force_login(self.admin)

    def test_state_changing_endpoints_reject_get(self):
        urls = (
            reverse('set_home_feed_mode'),
            reverse('record_view', args=[self.share.share_id]),
            reverse('record_copy', args=[self.share.share_id]),
            reverse('toggle_like', args=[self.share.share_id]),
            reverse('toggle_favorite', args=[self.share.share_id]),
            reverse('logout'),
            reverse('toggle_announcement_visibility', args=[self.announcement.pk]),
            reverse('open_site_message', args=[self.message.pk]),
            reverse('mark_all_site_messages_read'),
            reverse('admin_approve_share', args=[self.share.share_id]),
            reverse('admin_reject_share', args=[self.share.share_id]),
            reverse('admin_resolve_report', args=[self.report.pk, 'dismiss']),
            reverse('admin_resolve_share_reports', args=[self.share.share_id, 'dismiss']),
            reverse(
                'remove_share_from_collection',
                args=[self.collection.pk, self.share.share_id],
            ),
        )

        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 405)

        self.share.refresh_from_db()
        self.announcement.refresh_from_db()
        self.message.refresh_from_db()
        self.assertEqual(self.share.status, Share.Status.PENDING)
        self.assertTrue(self.announcement.is_active)
        self.assertIsNone(self.message.read_at)
        self.assertTrue(CollectionItem.objects.filter(collection=self.collection).exists())
        self.assertTrue(self.client.session.get('_auth_user_id'))

    def test_share_detail_get_is_read_only_and_record_view_is_deduplicated(self):
        detail_url = reverse('share_detail', args=[self.share.share_id])
        record_url = reverse('record_view', args=[self.share.share_id])

        self.assertEqual(self.client.get(detail_url).status_code, 200)
        self.share.refresh_from_db()
        self.assertEqual(self.share.views, 0)

        first = self.client.post(record_url)
        second = self.client.post(record_url)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.share.refresh_from_db()
        self.assertEqual(self.share.views, 1)

    def test_state_changing_post_requires_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin)

        response = csrf_client.post(reverse('logout'))

        self.assertEqual(response.status_code, 403)
        self.assertTrue(csrf_client.session.get('_auth_user_id'))


class SafeRedirectContractTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='author', password='password123')
        self.client.force_login(self.user)

    def collection_form_data(self, title):
        return {
            'title': title,
            'description': '',
            'is_public': 'on',
        }

    def test_create_collection_rejects_external_next_target(self):
        response = self.client.post(
            reverse('create_collection') + '?next=https://example.net/phishing',
            self.collection_form_data('外部重定向测试'),
        )

        self.assertRedirects(response, reverse('my_shares'))

    def test_create_collection_accepts_same_host_next_target(self):
        response = self.client.post(
            reverse('create_collection') + '?next=/profile/edit/',
            self.collection_form_data('站内重定向测试'),
        )

        self.assertRedirects(response, reverse('profile_edit'))
