from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .forms import AccountLoginForm, AccountRegistrationForm, CustomPasswordChangeForm
from .models import UserProfile
from .rate_limits import RateLimitResult


class AccountFormSemanticsTests(SimpleTestCase):
    def test_auth_forms_preserve_django_password_field_semantics(self):
        registration = AccountRegistrationForm()
        login = AccountLoginForm()
        password_change = CustomPasswordChangeForm(user=object())

        for form, field_names in (
            (registration, ('password1', 'password2')),
            (login, ('password',)),
            (password_change, ('old_password', 'new_password1', 'new_password2')),
        ):
            for field_name in field_names:
                with self.subTest(form=form.__class__.__name__, field=field_name):
                    self.assertFalse(form.fields[field_name].strip)
                    self.assertIn('form-control', form.fields[field_name].widget.attrs['class'])

        self.assertEqual(
            login.fields['password'].widget.attrs['autocomplete'],
            'current-password',
        )
        self.assertEqual(
            password_change.fields['old_password'].widget.attrs['autocomplete'],
            'current-password',
        )
        for field_name in ('password1', 'password2'):
            self.assertEqual(
                registration.fields[field_name].widget.attrs['autocomplete'],
                'new-password',
            )
        for field_name in ('new_password1', 'new_password2'):
            self.assertEqual(
                password_change.fields[field_name].widget.attrs['autocomplete'],
                'new-password',
            )
        self.assertTrue(password_change.fields['new_password1'].help_text)


@override_settings(RATE_LIMIT_ENABLED=False)
class AccountWorkflowContractTests(TestCase):
    credentials = {
        'username': 'new-account',
        'password1': 'ComplexPassword123!',
        'password2': 'ComplexPassword123!',
    }

    def setUp(self):
        cache.clear()

    def test_login_and_registration_contexts_preserve_safe_return_target(self):
        return_url = f"{reverse('profile_edit')}?section=account&source=registration"
        login_url = f"{reverse('login')}?{urlencode({'next': return_url})}"
        register_url = f"{reverse('register')}?{urlencode({'next': return_url})}"

        login_response = self.client.get(login_url)
        registration_response = self.client.get(register_url)

        self.assertEqual(login_response.context['next'], return_url)
        self.assertEqual(registration_response.context['next'], return_url)
        self.assertEqual(
            parse_qs(urlsplit(login_response.context['register_url']).query)['next'],
            [return_url],
        )
        self.assertEqual(
            parse_qs(urlsplit(registration_response.context['login_url']).query)['next'],
            [return_url],
        )
        self.assertContains(
            login_response,
            f'href="{login_response.context["register_url"]}"',
        )
        self.assertContains(
            registration_response,
            f'href="{registration_response.context["login_url"]}"',
        )
        self.assertContains(
            login_response,
            'name="next" value="/profile/edit/?section=account&amp;source=registration"',
        )
        self.assertContains(
            registration_response,
            'name="next" value="/profile/edit/?section=account&amp;source=registration"',
        )

    def test_registration_redirects_to_safe_return_target_and_ensures_profile(self):
        return_url = f"{reverse('profile_edit')}?source=registration"
        register_url = f"{reverse('register')}?{urlencode({'next': return_url})}"

        with patch.object(
            UserProfile.objects,
            'get_or_create',
            wraps=UserProfile.objects.get_or_create,
        ) as ensure_profile:
            response = self.client.post(register_url, self.credentials)

        user = User.objects.get(username=self.credentials['username'])
        self.assertRedirects(response, return_url, fetch_redirect_response=False)
        self.assertEqual(int(self.client.session['_auth_user_id']), user.pk)
        self.assertTrue(UserProfile.objects.filter(user=user).exists())
        ensure_profile.assert_called_once_with(user=user)

    def test_unsafe_registration_post_target_does_not_fall_back_to_safe_get_target(self):
        safe_target = reverse('profile_edit')
        register_url = f"{reverse('register')}?{urlencode({'next': safe_target})}"

        response = self.client.post(register_url, {
            **self.credentials,
            'username': 'unsafe-return-account',
            'next': 'https://example.net/phishing',
        })

        self.assertRedirects(
            response,
            settings.LOGIN_REDIRECT_URL,
            fetch_redirect_response=False,
        )

    def test_registration_rolls_back_user_when_profile_guarantee_fails(self):
        with patch.object(
            UserProfile.objects,
            'get_or_create',
            side_effect=RuntimeError('profile creation failed'),
        ):
            with self.assertRaisesMessage(RuntimeError, 'profile creation failed'):
                self.client.post(reverse('register'), {
                    **self.credentials,
                    'username': 'rolled-back-account',
                })

        self.assertFalse(User.objects.filter(username='rolled-back-account').exists())

    def test_password_change_preserves_significant_spaces_and_current_session(self):
        current_password = '  Current Password 123!  '
        new_password = '  QuartzNebula!4827River  '
        user = User.objects.create_user(
            username='password-session-owner',
            password=current_password,
        )
        self.client.force_login(user)

        response = self.client.post(reverse('password_change'), {
            'old_password': current_password,
            'new_password1': new_password,
            'new_password2': new_password,
        })

        self.assertRedirects(response, reverse('profile_edit'))
        user.refresh_from_db()
        self.assertTrue(user.check_password(new_password))
        self.assertFalse(user.check_password(new_password.strip()))
        self.assertEqual(int(self.client.session['_auth_user_id']), user.pk)

        self.client.post(reverse('logout'))
        stripped_login = self.client.post(reverse('login'), {
            'username': user.username,
            'password': new_password.strip(),
        })
        exact_login = self.client.post(reverse('login'), {
            'username': user.username,
            'password': new_password,
        })
        self.assertEqual(stripped_login.status_code, 200)
        self.assertRedirects(
            exact_login,
            settings.LOGIN_REDIRECT_URL,
            fetch_redirect_response=False,
        )
        self.assertEqual(int(self.client.session['_auth_user_id']), user.pk)

    def test_password_confirmation_treats_whitespace_as_significant(self):
        current_password = 'CurrentPassword123!'
        user = User.objects.create_user(
            username='password-whitespace-owner',
            password=current_password,
        )
        self.client.force_login(user)

        response = self.client.post(reverse('password_change'), {
            'old_password': current_password,
            'new_password1': 'QuartzNebula!4827River',
            'new_password2': ' QuartzNebula!4827River ',
        })

        self.assertEqual(response.status_code, 200)
        self.assertIn('new_password2', response.context['form'].errors)
        user.refresh_from_db()
        self.assertTrue(user.check_password(current_password))


@override_settings(RATE_LIMIT_ENABLED=False)
class AccountSecurityContractTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='account-security-user',
            password='CurrentPassword123!',
        )

    def test_account_pages_are_never_cached(self):
        for url_name in ('login', 'register'):
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))
                self.assertIn('no-store', response.headers['Cache-Control'])
                self.assertIn('private', response.headers['Cache-Control'])

        self.client.force_login(self.user)
        for url_name in ('profile_edit', 'password_change'):
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))
                self.assertIn('no-store', response.headers['Cache-Control'])
                self.assertIn('private', response.headers['Cache-Control'])

    def test_logout_response_is_never_cached(self):
        self.client.force_login(self.user)

        response = self.client.post(reverse('logout'))

        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertIn('private', response.headers['Cache-Control'])

    def test_account_form_views_reject_unsupported_methods(self):
        allowed_methods = 'GET, HEAD, POST'
        for url_name in ('login', 'register'):
            with self.subTest(url_name=url_name):
                self.assertEqual(self.client.head(reverse(url_name)).status_code, 200)
                for method_name in ('put', 'patch', 'delete', 'options'):
                    response = getattr(self.client, method_name)(reverse(url_name))
                    self.assertEqual(response.status_code, 405)
                    self.assertEqual(response.headers['Allow'], allowed_methods)

        self.client.force_login(self.user)
        for url_name in ('profile_edit', 'password_change'):
            with self.subTest(url_name=url_name):
                self.assertEqual(self.client.head(reverse(url_name)).status_code, 200)
                for method_name in ('put', 'patch', 'delete', 'options'):
                    response = getattr(self.client, method_name)(reverse(url_name))
                    self.assertEqual(response.status_code, 405)
                    self.assertEqual(response.headers['Allow'], allowed_methods)

    def test_account_post_endpoints_require_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        anonymous_cases = (
            ('login', {'username': self.user.username, 'password': 'wrong-password'}),
            ('register', {
                'username': 'csrf-registration',
                'password1': 'ComplexPassword123!',
                'password2': 'ComplexPassword123!',
            }),
        )
        for url_name, payload in anonymous_cases:
            with self.subTest(url_name=url_name):
                response = csrf_client.post(reverse(url_name), payload)
                self.assertEqual(response.status_code, 403)

        csrf_client.force_login(self.user)
        authenticated_cases = (
            ('profile_edit', {
                'nickname': 'CSRF',
                'bio': '',
                'home_feed_mode': 'infinite',
            }),
            ('password_change', {
                'old_password': 'CurrentPassword123!',
                'new_password1': 'DifferentPassword123!',
                'new_password2': 'DifferentPassword123!',
            }),
            ('logout', {}),
        )
        for url_name, payload in authenticated_cases:
            with self.subTest(url_name=url_name):
                response = csrf_client.post(reverse(url_name), payload)
                self.assertEqual(response.status_code, 403)

    def test_password_posts_are_marked_sensitive(self):
        login_response = self.client.post(reverse('login'), {
            'username': self.user.username,
            'password': 'wrong-password',
        })
        registration_response = self.client.post(reverse('register'), {
            'username': 'invalid-registration',
            'password1': 'first-password',
            'password2': 'different-password',
        })

        self.client.force_login(self.user)
        password_response = self.client.post(reverse('password_change'), {
            'old_password': 'wrong-password',
            'new_password1': 'DifferentPassword123!',
            'new_password2': 'DifferentPassword123!',
        })

        self.assertEqual(
            login_response.wsgi_request.sensitive_post_parameters,
            ('password',),
        )
        self.assertEqual(
            registration_response.wsgi_request.sensitive_post_parameters,
            ('password1', 'password2'),
        )
        self.assertEqual(
            password_response.wsgi_request.sensitive_post_parameters,
            ('old_password', 'new_password1', 'new_password2'),
        )


@override_settings(
    RATE_LIMIT_ENABLED=True,
    RATE_LIMIT_RULES={
        'register_ip': (0, 60 * 60),
        'login_ip': (0, 60 * 60),
        'login_account': (0, 60 * 60),
        'password_change_ip': (0, 60 * 60),
        'password_change_user': (0, 60 * 60),
    },
)
class AccountRateLimitContractTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='rate-limited-password-user',
            password='CurrentPassword123!',
        )

    def assert_rate_limited(self, response, expected_username=None):
        self.assertEqual(response.status_code, 429)
        self.assertGreater(int(response.headers['Retry-After']), 0)
        self.assertFalse(response.context['form'].is_bound)
        if expected_username is not None:
            self.assertEqual(response.context['form'].initial['username'], expected_username)
        self.assertIn('no-store', response.headers['Cache-Control'])

    def test_rate_limited_login_does_not_run_authentication_validation(self):
        submitted_password = 'login-secret-must-not-render'
        with (
            patch('django.contrib.auth.forms.authenticate') as authenticate,
            patch.object(AccountLoginForm, 'clean', autospec=True) as clean,
        ):
            response = self.client.post(reverse('login'), {
                'username': 'limited-login',
                'password': submitted_password,
            })

        self.assert_rate_limited(response, 'limited-login')
        self.assertNotContains(response, submitted_password, status_code=429)
        authenticate.assert_not_called()
        clean.assert_not_called()

    def test_retry_after_covers_an_exhausted_ip_window_at_its_limit(self):
        with patch(
            'shares.web.accounts.consume_rate_limit',
            side_effect=(
                RateLimitResult(True, 1, 1, 300),
                RateLimitResult(False, 2, 1, 60),
            ),
        ):
            response = self.client.post(reverse('login'), {
                'username': 'limited-login',
                'password': 'should-not-be-checked',
            })

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers['Retry-After'], '300')

    @override_settings(RATE_LIMIT_ENABLED=False)
    def test_login_account_limit_uses_django_username_normalization(self):
        allowed = RateLimitResult(True, 0, 10, 60)
        with patch(
            'shares.web.accounts.consume_rate_limit',
            return_value=allowed,
        ) as consume:
            response = self.client.post(reverse('login'), {
                'username': 'ａｄｍｉｎ',
                'password': 'wrong-password',
            })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            consume.call_args_list[1].args,
            ('login_account', 'account:admin'),
        )

    def test_login_limits_bound_an_oversized_username_before_reflection_and_hashing(self):
        oversized_username = 'ａ' * 5000

        ip_limited_response = self.client.post(reverse('login'), {
            'username': oversized_username,
            'password': 'wrong-password',
        })

        self.assertEqual(ip_limited_response.status_code, 429)
        self.assertEqual(
            len(ip_limited_response.context['form'].initial['username']),
            150,
        )

        with patch(
            'shares.web.accounts.consume_rate_limit',
            side_effect=(
                RateLimitResult(True, 1, 10, 300),
                RateLimitResult(False, 2, 1, 60),
            ),
        ) as consume:
            account_limited_response = self.client.post(reverse('login'), {
                'username': oversized_username,
                'password': 'wrong-password',
            })

        self.assertEqual(account_limited_response.status_code, 429)
        account_identity = consume.call_args_list[1].args[1]
        self.assertTrue(account_identity.startswith('account:'))
        self.assertLessEqual(len(account_identity.removeprefix('account:')), 150)

    def test_rate_limited_registration_does_not_run_form_validation(self):
        submitted_password = 'registration-secret-must-not-render'
        with patch.object(
            AccountRegistrationForm,
            'clean_username',
            autospec=True,
        ) as clean_username:
            response = self.client.post(reverse('register'), {
                'username': 'limited-registration',
                'password1': submitted_password,
                'password2': submitted_password,
            })

        self.assert_rate_limited(response, 'limited-registration')
        self.assertNotContains(response, submitted_password, status_code=429)
        clean_username.assert_not_called()
        self.assertFalse(User.objects.filter(username='limited-registration').exists())

    def test_password_change_ip_limit_does_not_check_current_password(self):
        self.client.force_login(self.user)
        submitted_password = 'password-change-secret-must-not-render'
        with patch.object(
            CustomPasswordChangeForm,
            'clean_old_password',
            autospec=True,
        ) as clean_old_password:
            response = self.client.post(reverse('password_change'), {
                'old_password': submitted_password,
                'new_password1': submitted_password,
                'new_password2': submitted_password,
            })

        self.assert_rate_limited(response)
        self.assertNotContains(response, submitted_password, status_code=429)
        clean_old_password.assert_not_called()
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('CurrentPassword123!'))

    @override_settings(RATE_LIMIT_RULES={
        'password_change_ip': (10, 60 * 60),
        'password_change_user': (0, 60 * 60),
    })
    def test_password_change_user_limit_does_not_check_current_password(self):
        self.client.force_login(self.user)
        with patch.object(
            CustomPasswordChangeForm,
            'clean_old_password',
            autospec=True,
        ) as clean_old_password:
            response = self.client.post(reverse('password_change'), {
                'old_password': 'should-not-be-checked',
                'new_password1': 'DifferentPassword123!',
                'new_password2': 'DifferentPassword123!',
            })

        self.assert_rate_limited(response)
        clean_old_password.assert_not_called()
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('CurrentPassword123!'))
