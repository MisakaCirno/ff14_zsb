from datetime import timedelta
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.html import strip_tags

from .forms import AccountRegistrationForm, CustomPasswordChangeForm
from .models import UserProfile
from .rate_limits import RateLimitResult


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


def _visible_text(markup):
    return ' '.join(unescape(strip_tags(markup)).split())


class _PageProbe(HTMLParser):
    """Small DOM-like probe for relationships that Django's test helpers omit."""

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
        element = {
            'uid': len(self.elements),
            'tag': tag,
            'attrs': dict(attrs),
            'ancestors': tuple(item['uid'] for item in self._open_elements),
        }
        self.elements.append(element)

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


@override_settings(RATE_LIMIT_ENABLED=False)
class AccountPageUiContractTests(TestCase):
    password = 'CurrentPassword123!'

    def setUp(self):
        self.user = User.objects.create_user(
            username='account-ui-user',
            password=self.password,
        )

    def page(self, response):
        markup = response.content.decode(response.charset)
        probe = _PageProbe()
        probe.feed(markup)
        return markup, probe

    def assert_unique_page_heading(self, response):
        self.assertEqual(response.status_code, 200)
        _, probe = self.page(response)
        self.assertEqual(len(probe.matching(tag='h1')), 1)

    def assert_field_descriptions_resolve(self, response, field_names):
        _, probe = self.page(response)
        page_ids = {
            element['attrs']['id']
            for element in probe.elements
            if element['attrs'].get('id')
        }
        for field_name in field_names:
            with self.subTest(field=field_name):
                controls = [
                    element
                    for element in probe.elements
                    if element['tag'] in {'input', 'select', 'textarea'}
                    and element['attrs'].get('name') == field_name
                ]
                self.assertEqual(len(controls), 1)
                raw_described_by = controls[0]['attrs'].get(
                    'aria-describedby',
                )
                if raw_described_by is None:
                    continue
                described_by = raw_described_by.split()
                self.assertTrue(described_by)
                for target_id in described_by:
                    self.assertIn(target_id, page_ids)

    def assert_help_text_rendered(self, response, form, field_names):
        page_text = _visible_text(response.content.decode(response.charset))
        for field_name in field_names:
            help_text = _visible_text(str(form.fields[field_name].help_text))
            self.assertTrue(help_text, field_name)
            with self.subTest(field=field_name):
                self.assertIn(help_text, page_text)

    def assert_error_summary(self, response):
        _, probe = self.page(response)
        summaries = probe.matching(attribute='data-account-error-summary')
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]['attrs'].get('role'), 'alert')

    def assert_safe_return_state(self, response, *, target, switch_path):
        _, probe = self.page(response)
        hidden_targets = [
            element['attrs'].get('value')
            for element in probe.matching(tag='input')
            if element['attrs'].get('name') == 'next'
        ]
        self.assertEqual(hidden_targets, [target])

        switch_links = [
            element
            for element in probe.matching(tag='a')
            if urlsplit(element['attrs'].get('href', '')).path == switch_path
            and parse_qs(
                urlsplit(element['attrs'].get('href', '')).query,
            ).get('next') == [target]
        ]
        self.assertEqual(len(switch_links), 1)
        self.assertEqual(
            parse_qs(urlsplit(switch_links[0]['attrs']['href']).query).get('next'),
            [target],
        )

    def profile_payload(self, **overrides):
        self.user.profile.refresh_from_db()
        payload = {
            'nickname': self.user.profile.nickname,
            'bio': self.user.profile.bio,
            'home_feed_mode': self.user.profile.home_feed_mode,
            'version': self.user.profile.updated_at.isoformat(),
        }
        payload.update(overrides)
        return payload

    def test_each_account_page_has_one_h1(self):
        for url_name in ('login', 'register'):
            with self.subTest(url_name=url_name):
                self.assert_unique_page_heading(self.client.get(reverse(url_name)))

        self.client.force_login(self.user)
        for url_name in ('profile_edit', 'password_change'):
            with self.subTest(url_name=url_name):
                self.assert_unique_page_heading(self.client.get(reverse(url_name)))

    def test_shared_account_fields_reference_descriptions_on_the_same_page(self):
        anonymous_cases = (
            ('login', ('username', 'password')),
            ('register', ('username', 'password1', 'password2')),
        )
        for url_name, field_names in anonymous_cases:
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))
                self.assert_field_descriptions_resolve(response, field_names)

        self.client.force_login(self.user)
        authenticated_cases = (
            ('profile_edit', ('nickname', 'bio', 'home_feed_mode')),
            (
                'password_change',
                ('old_password', 'new_password1', 'new_password2'),
            ),
        )
        for url_name, field_names in authenticated_cases:
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))
                self.assert_field_descriptions_resolve(response, field_names)

    def test_django_auth_help_text_remains_visible_after_validation_errors(self):
        registration_fields = ('username', 'password1', 'password2')
        registration_form = AccountRegistrationForm()
        registration_responses = (
            self.client.get(reverse('register')),
            self.client.post(reverse('register'), {
                'username': self.user.username,
                'password1': 'short',
                'password2': 'different',
            }),
        )
        for response in registration_responses:
            with self.subTest(page='register', status=response.status_code):
                self.assertEqual(response.status_code, 200)
                self.assert_help_text_rendered(
                    response,
                    registration_form,
                    registration_fields,
                )

        self.client.force_login(self.user)
        password_fields = ('new_password1', 'new_password2')
        password_form = CustomPasswordChangeForm(user=self.user)
        password_responses = (
            self.client.get(reverse('password_change')),
            self.client.post(reverse('password_change'), {
                'old_password': 'wrong-current-password',
                'new_password1': 'short',
                'new_password2': 'different',
            }),
        )
        for response in password_responses:
            with self.subTest(page='password_change', status=response.status_code):
                self.assertEqual(response.status_code, 200)
                self.assert_help_text_rendered(
                    response,
                    password_form,
                    password_fields,
                )

    def test_password_controls_keep_browser_password_manager_semantics(self):
        cases = (
            (
                'login',
                {'password': 'current-password'},
                False,
            ),
            (
                'register',
                {
                    'password1': 'new-password',
                    'password2': 'new-password',
                },
                False,
            ),
            (
                'password_change',
                {
                    'old_password': 'current-password',
                    'new_password1': 'new-password',
                    'new_password2': 'new-password',
                },
                True,
            ),
        )
        for url_name, expectations, requires_login in cases:
            with self.subTest(url_name=url_name):
                if requires_login:
                    self.client.force_login(self.user)
                else:
                    self.client.logout()
                response = self.client.get(reverse(url_name))
                _, probe = self.page(response)
                for field_name, expected in expectations.items():
                    controls = [
                        element
                        for element in probe.matching(tag='input')
                        if element['attrs'].get('name') == field_name
                    ]
                    self.assertEqual(len(controls), 1)
                    self.assertEqual(
                        controls[0]['attrs'].get('autocomplete'),
                        expected,
                    )

    def test_validation_errors_use_the_shared_alert_summary(self):
        responses = (
            self.client.post(reverse('login'), {
                'username': self.user.username,
                'password': 'wrong-password',
            }),
            self.client.post(reverse('register'), {
                'username': self.user.username,
                'password1': 'short',
                'password2': 'different',
            }),
        )
        for response in responses:
            with self.subTest(template=response.templates[-1].name):
                self.assertEqual(response.status_code, 200)
                self.assert_error_summary(response)

        self.client.force_login(self.user)
        profile_response = self.client.post(
            reverse('profile_edit'),
            self.profile_payload(nickname='x' * 51),
        )
        password_response = self.client.post(reverse('password_change'), {
            'old_password': 'wrong-password',
            'new_password1': 'short',
            'new_password2': 'different',
        })
        for response in (profile_response, password_response):
            with self.subTest(template=response.templates[-1].name):
                self.assertEqual(response.status_code, 200)
                self.assert_error_summary(response)

    def test_safe_next_survives_login_and_registration_validation(self):
        target = f"{reverse('my_shares')}?tab=likes&source=account"
        login_url = f"{reverse('login')}?{urlencode({'next': target})}"
        login_response = self.client.post(login_url, {
            'username': self.user.username,
            'password': 'wrong-password',
            'next': target,
        })
        self.assertEqual(login_response.status_code, 200)
        self.assert_safe_return_state(
            login_response,
            target=target,
            switch_path=reverse('register'),
        )

        register_url = f"{reverse('register')}?{urlencode({'next': target})}"
        register_response = self.client.post(register_url, {
            'username': self.user.username,
            'password1': 'short',
            'password2': 'different',
            'next': target,
        })
        self.assertEqual(register_response.status_code, 200)
        self.assert_safe_return_state(
            register_response,
            target=target,
            switch_path=reverse('login'),
        )

    def test_safe_next_survives_login_and_registration_rate_limits(self):
        target = f"{reverse('my_shares')}?tab=favorites&source=limited"
        blocked = RateLimitResult(False, 2, 1, 87)
        cases = (
            (
                'login',
                'register',
                {'username': self.user.username, 'password': 'login-secret'},
            ),
            (
                'register',
                'login',
                {
                    'username': 'limited-registration',
                    'password1': 'register-secret-one',
                    'password2': 'register-secret-two',
                },
            ),
        )
        for url_name, switch_name, payload in cases:
            with self.subTest(url_name=url_name), patch(
                'shares.web.accounts.consume_rate_limit',
                return_value=blocked,
            ):
                url = f"{reverse(url_name)}?{urlencode({'next': target})}"
                response = self.client.post(url, {**payload, 'next': target})
                self.assertEqual(response.status_code, 429)
                self.assertGreater(int(response.headers['Retry-After']), 0)
                self.assert_safe_return_state(
                    response,
                    target=target,
                    switch_path=reverse(switch_name),
                )
                for field_name, value in payload.items():
                    if field_name.startswith('password'):
                        self.assertNotContains(response, value, status_code=429)

    def test_unsafe_next_is_never_rendered_or_forwarded(self):
        unsafe_target = 'https://attacker.example/collect?token=unsafe-return-token'
        cases = (
            ('login', 'register', {'username': '', 'password': 'wrong'}),
            (
                'register',
                'login',
                {
                    'username': '',
                    'password1': 'first',
                    'password2': 'second',
                },
            ),
        )
        for url_name, switch_name, payload in cases:
            request_url = (
                f"{reverse(url_name)}?{urlencode({'next': unsafe_target})}"
            )
            for method in ('get', 'post'):
                with self.subTest(url_name=url_name, method=method):
                    if method == 'get':
                        response = self.client.get(request_url)
                    else:
                        response = self.client.post(
                            request_url,
                            {**payload, 'next': unsafe_target},
                        )
                    self.assertEqual(response.status_code, 200)
                    markup, probe = self.page(response)
                    self.assertNotIn(unsafe_target, markup)
                    self.assertFalse([
                        element
                        for element in probe.matching(tag='input')
                        if element['attrs'].get('name') == 'next'
                    ])
                    switch_links = [
                        element
                        for element in probe.matching(tag='a')
                        if urlsplit(element['attrs'].get('href', '')).path
                        == reverse(switch_name)
                    ]
                    self.assertTrue(switch_links)
                    for switch_link in switch_links:
                        self.assertNotIn(
                            'next',
                            parse_qs(
                                urlsplit(
                                    switch_link['attrs']['href'],
                                ).query,
                            ),
                        )

    def test_all_rate_limited_password_posts_hide_secrets(self):
        blocked = RateLimitResult(False, 3, 1, 91)
        anonymous_cases = (
            (
                'login',
                {
                    'username': self.user.username,
                    'password': 'limited-login-secret',
                },
            ),
            (
                'register',
                {
                    'username': 'limited-new-user',
                    'password1': 'limited-register-secret-one',
                    'password2': 'limited-register-secret-two',
                },
            ),
        )
        for url_name, payload in anonymous_cases:
            with self.subTest(url_name=url_name), patch(
                'shares.web.accounts.consume_rate_limit',
                return_value=blocked,
            ):
                response = self.client.post(reverse(url_name), payload)
                self.assertEqual(response.status_code, 429)
                self.assertGreater(int(response.headers['Retry-After']), 0)
                for field_name, value in payload.items():
                    if field_name.startswith('password'):
                        self.assertNotContains(response, value, status_code=429)

        self.client.force_login(self.user)
        password_payload = {
            'old_password': 'limited-current-secret',
            'new_password1': 'limited-new-secret-one',
            'new_password2': 'limited-new-secret-two',
        }
        with patch(
            'shares.web.accounts.consume_rate_limit',
            return_value=blocked,
        ):
            response = self.client.post(
                reverse('password_change'),
                password_payload,
            )
        self.assertEqual(response.status_code, 429)
        self.assertGreater(int(response.headers['Retry-After']), 0)
        for value in password_payload.values():
            self.assertNotContains(response, value, status_code=429)

    def test_profile_version_conflicts_offer_a_get_refresh_without_writing(self):
        self.client.force_login(self.user)

        stale_version = self.user.profile.updated_at
        UserProfile.objects.filter(pk=self.user.profile.pk).update(
            nickname='server-winner',
            bio='authoritative biography',
            updated_at=timezone.now() + timedelta(seconds=1),
        )
        self.user.profile.refresh_from_db()
        cases = (
            ('stale', stale_version.isoformat()),
            ('missing', None),
            ('invalid', 'not-a-profile-version'),
        )
        for case_name, version in cases:
            with self.subTest(case=case_name):
                self.user.profile.refresh_from_db()
                expected = (
                    self.user.profile.nickname,
                    self.user.profile.bio,
                    self.user.profile.home_feed_mode,
                    self.user.profile.updated_at,
                )
                payload = {
                    'nickname': f'client-{case_name}',
                    'bio': f'client biography {case_name}',
                    'home_feed_mode': UserProfile.HomeFeedMode.PAGINATED,
                }
                if version is not None:
                    payload['version'] = version

                response = self.client.post(reverse('profile_edit'), payload)

                self.assertEqual(response.status_code, 409)
                self.assert_error_summary(response)
                _, probe = self.page(response)
                summary = probe.matching(
                    attribute='data-account-error-summary',
                )[0]
                refresh_links = [
                    element
                    for element in probe.descendants(summary, tag='a')
                    if urlsplit(element['attrs'].get('href', '')).path
                    == reverse('profile_edit')
                ]
                self.assertTrue(refresh_links)
                self.assertFalse([
                    element
                    for element in probe.elements
                    if 'alert-success'
                    in element['attrs'].get('class', '').split()
                ])

                self.user.profile.refresh_from_db()
                actual = (
                    self.user.profile.nickname,
                    self.user.profile.bio,
                    self.user.profile.home_feed_mode,
                    self.user.profile.updated_at,
                )
                self.assertEqual(actual, expected)

    def test_account_settings_navigation_marks_only_the_current_item(self):
        self.client.force_login(self.user)
        managed_paths = {
            reverse('profile_edit'),
            reverse('password_change'),
            reverse('my_shares'),
        }
        for url_name in ('profile_edit', 'password_change'):
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))
                _, probe = self.page(response)
                settings_nav = [
                    element
                    for element in probe.matching(tag='nav')
                    if 'data-account-settings-nav' in element['attrs']
                ]
                self.assertEqual(len(settings_nav), 1)
                current_links = [
                    element
                    for element in probe.descendants(settings_nav[0], tag='a')
                    if element['attrs'].get('href') in managed_paths
                    and element['attrs'].get('aria-current') == 'page'
                ]
                self.assertEqual(len(current_links), 1)
                self.assertEqual(
                    current_links[0]['attrs'].get('href'),
                    reverse(url_name),
                )

    def test_base_logout_is_post_with_csrf_and_has_noscript_account_nav(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('index'))
        _, probe = self.page(response)
        logout_path = reverse('logout')

        self.assertFalse([
            element
            for element in probe.matching(tag='a')
            if urlsplit(element['attrs'].get('href', '')).path == logout_path
        ])
        logout_forms = [
            element
            for element in probe.matching(tag='form')
            if urlsplit(element['attrs'].get('action', '')).path == logout_path
        ]
        self.assertTrue(logout_forms)
        for logout_form in logout_forms:
            self.assertEqual(
                logout_form['attrs'].get('method', '').lower(),
                'post',
            )
            csrf_inputs = [
                element
                for element in probe.descendants(logout_form, tag='input')
                if element['attrs'].get('name') == 'csrfmiddlewaretoken'
                and element['attrs'].get('value')
            ]
            self.assertEqual(len(csrf_inputs), 1)

        noscript_elements = probe.matching(tag='noscript')
        self.assertEqual(len(noscript_elements), 1)
        noscript_descendants = probe.descendants(noscript_elements[0])
        self.assertTrue(any(
            element['tag'] == 'nav'
            and element['attrs'].get('aria-label')
            for element in noscript_descendants
        ))
        noscript_paths = {
            urlsplit(element['attrs'].get('href', '')).path
            for element in noscript_descendants
            if element['tag'] == 'a'
        }
        self.assertTrue({
            reverse('profile_edit'),
            reverse('password_change'),
            reverse('my_shares'),
        }.issubset(noscript_paths))

    def test_core_content_management_routes_remain_reachable(self):
        self.client.force_login(self.user)
        index_response = self.client.get(reverse('index'))
        _, index_probe = self.page(index_response)
        index_paths = {
            urlsplit(element['attrs'].get('href', '')).path
            for element in index_probe.matching(tag='a')
        }
        self.assertTrue({
            reverse('my_shares'),
            reverse('create_share'),
        }.issubset(index_paths))

        for url_name in ('profile_edit', 'password_change'):
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))
                _, probe = self.page(response)
                self.assertTrue(any(
                    element['attrs'].get('href') == reverse('my_shares')
                    for element in probe.matching(tag='a')
                ))
