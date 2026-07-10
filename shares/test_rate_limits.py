from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .models import Report, Share
from .rate_limits import consume_rate_limit, get_client_ip


TEST_RULES = {
    'register_ip': (1, 60 * 60),
    'login_ip': (1, 60 * 60),
    'login_account': (1, 60 * 60),
    'anonymous_create_ip': (1, 60 * 60),
    'authenticated_create_user': (1, 60 * 60),
    'report_user': (1, 60 * 60),
    'view_counter_ip': (1, 60 * 60),
    'copy_counter_ip': (1, 60 * 60),
    'test_rule': (2, 60),
}


@override_settings(RATE_LIMIT_ENABLED=True, RATE_LIMIT_RULES=TEST_RULES)
class RateLimitPrimitiveTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_fixed_window_limit_allows_then_rejects(self):
        first = consume_rate_limit('test_rule', 'identity', now=120)
        second = consume_rate_limit('test_rule', 'identity', now=120)
        third = consume_rate_limit('test_rule', 'identity', now=120)
        next_window = consume_rate_limit('test_rule', 'identity', now=180)

        self.assertTrue(first.allowed)
        self.assertTrue(second.allowed)
        self.assertFalse(third.allowed)
        self.assertTrue(next_window.allowed)

    @override_settings(TRUST_X_FORWARDED_FOR=True)
    def test_trusted_forwarded_ip_uses_first_canonical_address(self):
        request = RequestFactory().get(
            '/',
            HTTP_X_FORWARDED_FOR='203.0.113.7, 10.0.0.1',
            REMOTE_ADDR='127.0.0.1',
        )

        self.assertEqual(get_client_ip(request), '203.0.113.7')

    @override_settings(TRUST_X_FORWARDED_FOR=False)
    def test_untrusted_forwarded_ip_is_ignored(self):
        request = RequestFactory().get(
            '/',
            HTTP_X_FORWARDED_FOR='203.0.113.7',
            REMOTE_ADDR='127.0.0.1',
        )

        self.assertEqual(get_client_ip(request), '127.0.0.1')


@override_settings(RATE_LIMIT_ENABLED=True, RATE_LIMIT_RULES=TEST_RULES)
class RateLimitedWorkflowTests(TestCase):
    def setUp(self):
        cache.clear()
        self.author = User.objects.create_user(username='author', password='password123')
        self.user = User.objects.create_user(username='user', password='password123')
        self.share = Share.objects.create(
            title='限流测试',
            strategy_code='[stgy:rate-limit]',
            author=self.author,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )

    def share_form_data(self, title='匿名分享'):
        return {
            'title': title,
            'strategy_code': '[stgy:anonymous-rate-limit]',
            'description': '',
            'category': Share.Category.ENTERTAINMENT,
            'visibility': Share.Visibility.PUBLIC,
        }

    def test_registration_is_limited_by_client_ip(self):
        first = self.client.post(reverse('register'), {
            'username': 'first-user',
            'password1': 'ComplexPassword123!',
            'password2': 'ComplexPassword123!',
        })
        second = Client().post(reverse('register'), {
            'username': 'second-user',
            'password1': 'ComplexPassword123!',
            'password2': 'ComplexPassword123!',
        })

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 429)
        self.assertFalse(User.objects.filter(username='second-user').exists())

    def test_login_attempts_are_limited(self):
        first = self.client.post(reverse('login'), {
            'username': self.user.username,
            'password': 'wrong-password',
        })
        second = self.client.post(reverse('login'), {
            'username': self.user.username,
            'password': 'wrong-password',
        })

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)

    def test_anonymous_share_creation_is_limited(self):
        first = self.client.post(reverse('create_share'), self.share_form_data('第一个分享'))
        second = self.client.post(reverse('create_share'), self.share_form_data('第二个分享'))

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 429)
        self.assertFalse(Share.objects.filter(title='第二个分享').exists())

    def test_reports_are_limited_per_user(self):
        self.client.force_login(self.user)

        first = self.client.post(
            reverse('report_share', args=[self.share.share_id]),
            {'reason': '第一次举报'},
        )
        second = self.client.post(
            reverse('report_share', args=[self.share.share_id]),
            {'reason': '第二次举报'},
        )

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(Report.objects.filter(reporter=self.user).count(), 1)

    def test_view_and_copy_counter_bursts_are_suppressed(self):
        other_share = Share.objects.create(
            title='第二个计数目标',
            strategy_code='[stgy:second-counter]',
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )

        self.client.post(reverse('record_view', args=[self.share.share_id]))
        second_view = self.client.post(reverse('record_view', args=[other_share.share_id]))
        self.client.post(reverse('record_copy', args=[self.share.share_id]))
        second_copy = self.client.post(reverse('record_copy', args=[other_share.share_id]))

        self.share.refresh_from_db()
        other_share.refresh_from_db()
        self.assertEqual(second_view.status_code, 200)
        self.assertEqual(second_copy.status_code, 200)
        self.assertEqual(self.share.views, 1)
        self.assertEqual(self.share.copies, 1)
        self.assertEqual(other_share.views, 0)
        self.assertEqual(other_share.copies, 0)
