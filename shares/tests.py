from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Report, Share, SiteMessage, UserProfile


class HomeFeedModeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='password123')
        for index in range(13):
            Share.objects.create(
                title=f'分享 {index}',
                strategy_code=f'[stgy:test-{index}]',
                author=self.user,
                visibility=Share.Visibility.PUBLIC,
                status=Share.Status.APPROVED,
            )

    def test_default_home_uses_waterfall_mode(self):
        response = self.client.get(reverse('index'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['feed_mode'], UserProfile.HomeFeedMode.INFINITE)
        self.assertContains(response, '瀑布')
        self.assertContains(response, 'id="infinite-scroll-sentinel"')
        self.assertIn(
            'shares/includes/share_cards.html',
            [template.name for template in response.templates],
        )
        self.assertIn('HX-Request', response.headers['Vary'])
        self.assertIn('Cookie', response.headers['Vary'])

    def test_get_feed_mode_choice_does_not_mutate_profile(self):
        self.client.login(username='alice', password='password123')
        self.user.profile.home_feed_mode = UserProfile.HomeFeedMode.PAGINATED
        self.user.profile.save(update_fields=['home_feed_mode'])

        response = self.client.get(reverse('index'), {'feed': UserProfile.HomeFeedMode.INFINITE})

        self.assertEqual(response.status_code, 200)
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.home_feed_mode, UserProfile.HomeFeedMode.PAGINATED)
        self.assertContains(response, 'id="infinite-scroll-sentinel"')

    def test_post_feed_mode_choice_is_saved(self):
        self.client.login(username='alice', password='password123')
        self.user.profile.home_feed_mode = UserProfile.HomeFeedMode.PAGINATED
        self.user.profile.save(update_fields=['home_feed_mode'])

        response = self.client.post(reverse('set_home_feed_mode'), {
            'feed': UserProfile.HomeFeedMode.INFINITE,
            'next': '/?feed=infinite',
        })

        self.assertRedirects(response, '/?feed=infinite')
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.home_feed_mode, UserProfile.HomeFeedMode.INFINITE)

    def test_share_partial_returns_next_page_cards(self):
        response = self.client.get(reverse('index'), {
            'feed': UserProfile.HomeFeedMode.INFINITE,
            'partial': 'shares',
            'page': 2,
        })

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('html', data)
        self.assertFalse(data['has_next'])
        self.assertIsNone(data['next_page'])
        self.assertIn('card h-100', data['html'])
        self.assertIn('分享', data['html'])
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertIn('HX-Request', response.headers['Vary'])

    def test_share_partial_login_return_url_excludes_transport_parameters(self):
        response = self.client.get(reverse('index'), {
            'feed': UserProfile.HomeFeedMode.INFINITE,
            'partial': 'shares',
            'page': 2,
            'sort': 'likes',
        })

        html = response.json()['html']
        self.assertIn(
            'next=/%3Ffeed%3Dinfinite%26sort%3Dlikes',
            html,
        )
        self.assertNotIn('partial%3Dshares', html)
        self.assertNotIn('page%3D2', html)

    def test_hx_request_returns_html_fragment_and_takes_precedence(self):
        response = self.client.get(
            reverse('index'),
            {'partial': 'shares', 'page': 2},
            HTTP_HX_REQUEST='true',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers['Content-Type'].startswith('text/html'))
        self.assertContains(response, 'card h-100')
        self.assertNotContains(response, '<!DOCTYPE html>')
        self.assertNotContains(response, 'id="infinite-scroll-sentinel"')
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertIn('HX-Request', response.headers['Vary'])
        self.assertIn('Cookie', response.headers['Vary'])

    def test_home_and_search_reject_unsafe_methods(self):
        for url in (reverse('index'), reverse('search')):
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url).status_code, 405)


class SiteMessageWorkflowTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(username='author', password='password123')
        self.reporter = User.objects.create_user(username='reporter', password='password123')
        self.admin = User.objects.create_user(username='admin', password='password123', is_staff=True)

    def test_reject_share_creates_feedback_message(self):
        share = Share.objects.create(
            title='待审核分享',
            strategy_code='[stgy:pending]',
            author=self.author,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.PENDING,
        )

        self.client.login(username='admin', password='password123')
        response = self.client.post(reverse('admin_reject_share', args=[share.share_id]), {
            'reason': '包含不适合公开展示的内容',
        })

        self.assertEqual(response.status_code, 302)
        share.refresh_from_db()
        self.assertEqual(share.status, Share.Status.REJECTED)
        self.assertEqual(share.visibility, Share.Visibility.PRIVATE)
        self.assertEqual(share.review_feedback, '包含不适合公开展示的内容')

        message = SiteMessage.objects.get(recipient=self.author)
        self.assertEqual(message.message_type, SiteMessage.MessageType.SHARE_REJECTED)
        self.assertIn('包含不适合公开展示的内容', message.content)
        self.assertEqual(message.related_share, share)

    def test_dismiss_report_creates_reporter_message(self):
        share = Share.objects.create(
            title='被举报分享',
            strategy_code='[stgy:reported]',
            author=self.author,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )
        report = Report.objects.create(
            share=share,
            reporter=self.reporter,
            reason='疑似违规',
        )

        self.client.login(username='admin', password='password123')
        response = self.client.post(reverse('admin_resolve_report', args=[report.id, 'dismiss']), {
            'reason': '核查后暂未发现违规内容',
        })

        self.assertEqual(response.status_code, 302)
        report.refresh_from_db()
        self.assertEqual(report.status, Report.Status.DISMISSED)
        self.assertEqual(report.resolution_reason, '核查后暂未发现违规内容')

        message = SiteMessage.objects.get(recipient=self.reporter)
        self.assertEqual(message.message_type, SiteMessage.MessageType.REPORT_DISMISSED)
        self.assertEqual(message.related_report, report)
        self.assertIn('核查后暂未发现违规内容', message.content)

    def test_site_message_is_only_marked_read_by_post_open_action(self):
        message = SiteMessage.objects.create(
            recipient=self.author,
            sender=self.admin,
            message_type=SiteMessage.MessageType.REPORT_RESOLVED,
            title='举报已处理',
            content='处理说明',
        )

        self.client.login(username='author', password='password123')
        detail_response = self.client.get(reverse('site_message_detail', args=[message.id]))

        self.assertEqual(detail_response.status_code, 200)
        message.refresh_from_db()
        self.assertIsNone(message.read_at)

        response = self.client.post(reverse('open_site_message', args=[message.id]))

        self.assertRedirects(response, reverse('site_message_detail', args=[message.id]))
        message.refresh_from_db()
        self.assertIsNotNone(message.read_at)
