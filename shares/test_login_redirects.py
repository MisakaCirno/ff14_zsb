from urllib.parse import parse_qs, urlencode, urlsplit

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse


@override_settings(RATE_LIMIT_ENABLED=False)
class LoginReturnUrlContractTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='return-user',
            password='password123',
        )
        self.credentials = {
            'username': self.user.username,
            'password': 'password123',
        }

    def test_login_required_round_trip_preserves_safe_query(self):
        protected_url = f"{reverse('profile_edit')}?section=account&source=detail"

        protected_response = self.client.get(protected_url)

        self.assertEqual(protected_response.status_code, 302)
        login_query = parse_qs(urlsplit(protected_response.url).query)
        self.assertEqual(login_query['next'], [protected_url])

        login_page = self.client.get(protected_response.url)
        self.assertEqual(login_page.context['next'], protected_url)
        self.assertContains(
            login_page,
            'name="next" value="/profile/edit/?section=account&amp;source=detail"',
        )

        login_response = self.client.post(protected_response.url, self.credentials)

        self.assertRedirects(
            login_response,
            protected_url,
            fetch_redirect_response=False,
        )

    def test_post_next_takes_precedence_over_get_next(self):
        get_target = f"{reverse('profile_edit')}?source=get"
        post_target = f"{reverse('site_message_list')}?source=post"
        login_url = f"{reverse('login')}?{urlencode({'next': get_target})}"

        response = self.client.post(
            login_url,
            {**self.credentials, 'next': post_target},
        )

        self.assertRedirects(response, post_target, fetch_redirect_response=False)

    def test_unsafe_post_next_does_not_fall_back_to_safe_get_next(self):
        get_target = reverse('profile_edit')
        login_url = f"{reverse('login')}?{urlencode({'next': get_target})}"

        response = self.client.post(
            login_url,
            {**self.credentials, 'next': 'https://example.net/phishing'},
        )

        self.assertRedirects(
            response,
            settings.LOGIN_REDIRECT_URL,
            fetch_redirect_response=False,
        )

    def test_unsafe_return_targets_fall_back_to_login_redirect_url(self):
        unsafe_targets = (
            'https://example.net/phishing',
            '//example.net/phishing',
            r'\\example.net\phishing',
            r'/\example.net/phishing',
            'profile/edit/',
            'javascript:alert(1)',
            'http://[invalid',
        )

        for return_url in unsafe_targets:
            with self.subTest(return_url=return_url):
                response = self.client.post(
                    reverse('login'),
                    {**self.credentials, 'next': return_url},
                )

                self.assertRedirects(
                    response,
                    settings.LOGIN_REDIRECT_URL,
                    fetch_redirect_response=False,
                )

    def test_secure_login_rejects_http_downgrade_on_same_host(self):
        response = self.client.post(
            reverse('login'),
            {
                **self.credentials,
                'next': f"http://testserver{reverse('profile_edit')}",
            },
            secure=True,
        )

        self.assertRedirects(
            response,
            settings.LOGIN_REDIRECT_URL,
            fetch_redirect_response=False,
        )

    def test_failed_login_preserves_validated_post_next(self):
        return_url = f"{reverse('profile_edit')}?section=account&source=detail"

        response = self.client.post(reverse('login'), {
            'username': self.user.username,
            'password': 'wrong-password',
            'next': return_url,
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['next'], return_url)
        self.assertContains(
            response,
            'name="next" value="/profile/edit/?section=account&amp;source=detail"',
        )

    @override_settings(
        RATE_LIMIT_ENABLED=True,
        RATE_LIMIT_RULES={
            'login_ip': (0, 60 * 60),
            'login_account': (0, 60 * 60),
        },
    )
    def test_rate_limited_login_preserves_validated_post_next(self):
        return_url = f"{reverse('profile_edit')}?section=account&source=detail"

        response = self.client.post(
            reverse('login'),
            {**self.credentials, 'next': return_url},
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.context['next'], return_url)
        self.assertContains(
            response,
            'name="next" value="/profile/edit/?section=account&amp;source=detail"',
            status_code=429,
        )
