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

    def test_authenticated_feed_mode_choice_is_saved(self):
        self.client.login(username='alice', password='password123')

        response = self.client.get(reverse('index'), {'feed': UserProfile.HomeFeedMode.INFINITE})

        self.assertEqual(response.status_code, 200)
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.home_feed_mode, UserProfile.HomeFeedMode.INFINITE)
        self.assertContains(response, 'id="infinite-scroll-sentinel"')

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

    def test_opening_site_message_marks_it_read(self):
        message = SiteMessage.objects.create(
            recipient=self.author,
            sender=self.admin,
            message_type=SiteMessage.MessageType.REPORT_RESOLVED,
            title='举报已处理',
            content='处理说明',
        )

        self.client.login(username='author', password='password123')
        response = self.client.get(reverse('site_message_detail', args=[message.id]))

        self.assertEqual(response.status_code, 200)
        message.refresh_from_db()
        self.assertIsNotNone(message.read_at)
