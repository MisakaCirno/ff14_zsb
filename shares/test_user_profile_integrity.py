from datetime import timedelta
from unittest.mock import Mock, patch

from django import forms
from django.contrib.admin import ModelAdmin
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.db import connection
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .admin import UserAdmin, UserProfileAdmin, UserProfileInline
from .admin_forms import UserProfileAdminForm
from .forms import UserProfileForm
from .models import Share, UserProfile
from .services.profiles import ProfileUnavailableError
from .signals import ensure_user_profile_on_creation
from .validation import PROFILE_BIO_MAX_LENGTH, PROFILE_MISSING_VERSION


User = get_user_model()


@override_settings(RATE_LIMIT_ENABLED=False)
class UserProfileLifecycleTests(TestCase):
    def setUp(self):
        self.password = 'CurrentPassword123!'
        self.user = User.objects.create_user(
            username='profile-lifecycle-user',
            password=self.password,
        )

    def freeze_profile_updated_at(self):
        frozen_at = timezone.now() - timedelta(days=7)
        UserProfile.objects.filter(user=self.user).update(updated_at=frozen_at)
        return frozen_at

    def test_normal_user_creation_uses_the_write_database_and_is_idempotent(self):
        with patch.object(
            UserProfile.objects,
            'using',
            wraps=UserProfile.objects.using,
        ) as use_database:
            user = User.objects.create_user(username='signal-created-user')

        use_database.assert_called_once_with('default')
        self.assertEqual(UserProfile.objects.filter(user=user).count(), 1)

        ensure_user_profile_on_creation(
            User,
            user,
            created=True,
            using='default',
        )
        self.assertEqual(UserProfile.objects.filter(user=user).count(), 1)

    def test_signal_propagates_a_non_default_database_alias(self):
        profile_queryset = Mock()
        with patch.object(
            UserProfile.objects,
            'using',
            return_value=profile_queryset,
        ) as use_database:
            ensure_user_profile_on_creation(
                User,
                self.user,
                created=True,
                using='archive',
            )

        use_database.assert_called_once_with('archive')
        profile_queryset.get_or_create.assert_called_once_with(
            user_id=self.user.pk,
        )

    def test_signal_skips_raw_fixture_saves(self):
        with patch.object(UserProfile.objects, 'using') as use_database:
            ensure_user_profile_on_creation(
                User,
                self.user,
                created=True,
                raw=True,
                using='default',
            )

        use_database.assert_not_called()

    def test_existing_user_save_does_not_create_or_save_a_profile(self):
        profile = self.user.profile
        original_updated_at = self.freeze_profile_updated_at()

        with patch.object(UserProfile.objects, 'using') as use_database:
            self.user.first_name = 'Updated'
            self.user.save(update_fields=['first_name'])

        use_database.assert_not_called()
        profile.refresh_from_db()
        self.assertEqual(profile.updated_at, original_updated_at)

    def test_login_does_not_touch_profile_updated_at(self):
        original_updated_at = self.freeze_profile_updated_at()

        response = self.client.post(reverse('login'), {
            'username': self.user.username,
            'password': self.password,
        })

        self.assertRedirects(response, '/', fetch_redirect_response=False)
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.updated_at, original_updated_at)

    def test_password_change_does_not_touch_profile_updated_at(self):
        original_updated_at = self.freeze_profile_updated_at()
        self.client.force_login(self.user)

        response = self.client.post(reverse('password_change'), {
            'old_password': self.password,
            'new_password1': 'QuartzNebula!4827River',
            'new_password2': 'QuartzNebula!4827River',
        })

        self.assertRedirects(response, reverse('profile_edit'))
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.updated_at, original_updated_at)

    def test_stale_cached_profile_is_not_written_back_when_user_is_saved(self):
        cached_profile = self.user.profile
        cached_profile.nickname = 'stale nickname'
        cached_profile.bio = 'stale biography'
        fresh_updated_at = timezone.now() - timedelta(hours=1)
        UserProfile.objects.filter(pk=cached_profile.pk).update(
            nickname='fresh nickname',
            bio='fresh biography',
            updated_at=fresh_updated_at,
        )

        self.user.last_name = 'Unrelated user change'
        self.user.save(update_fields=['last_name'])

        persisted = UserProfile.objects.get(pk=cached_profile.pk)
        self.assertEqual(persisted.nickname, 'fresh nickname')
        self.assertEqual(persisted.bio, 'fresh biography')
        self.assertEqual(persisted.updated_at, fresh_updated_at)

    def test_missing_profile_does_not_break_read_only_account_or_content_pages(self):
        share = Share.objects.create(
            title='缺失资料回退测试',
            strategy_code='[stgy:missing-profile]',
            author=self.user,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )
        UserProfile.objects.filter(user=self.user).delete()
        self.client.force_login(self.user)

        responses = (
            self.client.get(reverse('profile_edit')),
            self.client.get(reverse('index')),
            self.client.get(reverse('user_public_profile', args=[self.user.username])),
            self.client.get(reverse('share_detail', args=[share.share_id])),
        )

        for response in responses:
            with self.subTest(path=response.wsgi_request.path):
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, self.user.username)
        self.assertFalse(UserProfile.objects.filter(user=self.user).exists())


@override_settings(RATE_LIMIT_ENABLED=False)
class UserProfileEditIntegrityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='profile-editor')
        self.client.force_login(self.user)

    def payload(self, *, version=None, **overrides):
        profile = UserProfile.objects.get(user=self.user)
        values = {
            'version': version or profile.updated_at.isoformat(),
            'nickname': profile.nickname,
            'bio': profile.bio,
            'home_feed_mode': profile.home_feed_mode,
        }
        values.update(overrides)
        return values

    def test_get_renders_a_hidden_timestamp_version(self):
        profile = self.user.profile

        response = self.client.get(reverse('profile_edit'))

        form = response.context['form']
        self.assertIsInstance(form.fields['version'].widget, forms.HiddenInput)
        self.assertEqual(
            form.fields['version'].to_python(form['version'].value()),
            profile.updated_at,
        )
        self.assertContains(response, 'name="version"', html=False)

    def test_missing_and_invalid_versions_return_conflict_without_writing(self):
        profile = self.user.profile
        original = (profile.nickname, profile.bio, profile.updated_at)

        missing = self.payload(nickname='must not persist')
        missing.pop('version')
        missing_response = self.client.post(reverse('profile_edit'), missing)
        invalid_response = self.client.post(
            reverse('profile_edit'),
            self.payload(version='not-a-version', nickname='also rejected'),
        )

        self.assertEqual(missing_response.status_code, 409)
        self.assertIn('version', missing_response.context['form'].errors)
        self.assertContains(
            missing_response,
            '请刷新后重新提交',
            status_code=409,
        )
        self.assertEqual(invalid_response.status_code, 409)
        self.assertIn('version', invalid_response.context['form'].errors)
        self.assertContains(
            invalid_response,
            '请刷新后重新提交',
            status_code=409,
        )
        profile.refresh_from_db()
        self.assertEqual(
            (profile.nickname, profile.bio, profile.updated_at),
            original,
        )

    def test_stale_version_returns_conflict_and_preserves_every_newer_field(self):
        profile = self.user.profile
        stale_version = profile.updated_at.isoformat()
        newer_time = timezone.now() + timedelta(seconds=1)
        UserProfile.objects.filter(pk=profile.pk).update(
            nickname='newer nickname',
            bio='newer biography',
            home_feed_mode=UserProfile.HomeFeedMode.PAGINATED,
            updated_at=newer_time,
        )

        response = self.client.post(reverse('profile_edit'), {
            'version': stale_version,
            'nickname': 'stale nickname',
            'bio': 'stale biography',
            'home_feed_mode': UserProfile.HomeFeedMode.INFINITE,
        })

        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.context['form'].non_field_errors())
        profile.refresh_from_db()
        self.assertEqual(profile.nickname, 'newer nickname')
        self.assertEqual(profile.bio, 'newer biography')
        self.assertEqual(
            profile.home_feed_mode,
            UserProfile.HomeFeedMode.PAGINATED,
        )
        self.assertEqual(profile.updated_at, newer_time)

    def test_only_truly_changed_fields_are_saved(self):
        profile = self.user.profile
        recorded_update_fields = []
        original_save = UserProfile.save

        def recording_save(instance, *args, **kwargs):
            recorded_update_fields.append(kwargs.get('update_fields'))
            return original_save(instance, *args, **kwargs)

        with patch.object(UserProfile, 'save', new=recording_save):
            response = self.client.post(
                reverse('profile_edit'),
                self.payload(nickname='only nickname changed'),
            )

        self.assertRedirects(response, reverse('profile_edit'))
        self.assertEqual(recorded_update_fields, [['nickname', 'updated_at']])
        profile.refresh_from_db()
        self.assertEqual(profile.nickname, 'only nickname changed')

    def test_noop_submission_does_not_touch_updated_at(self):
        profile = self.user.profile
        original_updated_at = profile.updated_at

        response = self.client.post(
            reverse('profile_edit'),
            self.payload(),
            follow=True,
        )

        self.assertRedirects(response, reverse('profile_edit'))
        self.assertContains(response, '个人资料没有修改')
        profile.refresh_from_db()
        self.assertEqual(profile.updated_at, original_updated_at)

    def test_profile_unavailable_returns_a_visible_conflict_response(self):
        with patch(
            'shares.web.accounts.update_user_profile_from_form',
            side_effect=ProfileUnavailableError,
        ):
            response = self.client.post(
                reverse('profile_edit'),
                self.payload(nickname='must not persist'),
            )

        self.assertEqual(response.status_code, 409)
        self.assertContains(response, '请刷新后重试', status_code=409)
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.nickname, '')

    def test_missing_profile_get_uses_sentinel_and_post_creates_atomically(self):
        UserProfile.objects.filter(user=self.user).delete()

        get_response = self.client.get(reverse('profile_edit'))
        self.assertEqual(
            get_response.context['form']['version'].value(),
            PROFILE_MISSING_VERSION,
        )
        self.assertFalse(UserProfile.objects.filter(user=self.user).exists())

        post_response = self.client.post(reverse('profile_edit'), {
            'version': PROFILE_MISSING_VERSION,
            'nickname': 'restored profile',
            'bio': 'created only on POST',
            'home_feed_mode': UserProfile.HomeFeedMode.PAGINATED,
        })

        self.assertRedirects(post_response, reverse('profile_edit'))
        profile = UserProfile.objects.get(user=self.user)
        self.assertEqual(profile.nickname, 'restored profile')
        self.assertEqual(profile.bio, 'created only on POST')

    def test_missing_sentinel_conflicts_if_another_writer_created_the_profile(self):
        UserProfile.objects.filter(user=self.user).delete()
        self.client.get(reverse('profile_edit'))
        concurrent = UserProfile.objects.create(
            user=self.user,
            nickname='concurrent winner',
            bio='must remain',
        )

        response = self.client.post(reverse('profile_edit'), {
            'version': PROFILE_MISSING_VERSION,
            'nickname': 'stale missing page',
            'bio': 'must not overwrite',
            'home_feed_mode': UserProfile.HomeFeedMode.PAGINATED,
        })

        self.assertEqual(response.status_code, 409)
        concurrent.refresh_from_db()
        self.assertEqual(concurrent.nickname, 'concurrent winner')
        self.assertEqual(concurrent.bio, 'must remain')

    def test_unchanged_legacy_long_bio_survives_another_field_edit(self):
        profile = self.user.profile
        legacy_bio = '旧' * (PROFILE_BIO_MAX_LENGTH + 37)
        UserProfile.objects.filter(pk=profile.pk).update(
            bio=legacy_bio,
            updated_at=timezone.now(),
        )
        profile.refresh_from_db()

        response = self.client.post(
            reverse('profile_edit'),
            self.payload(nickname='new nickname'),
        )

        self.assertRedirects(response, reverse('profile_edit'))
        profile.refresh_from_db()
        self.assertEqual(profile.nickname, 'new nickname')
        self.assertEqual(profile.bio, legacy_bio)

    def test_unchanged_legacy_whitespace_survives_another_field_edit(self):
        profile = self.user.profile
        legacy_nickname = '  legacy nickname  '
        legacy_bio = '  legacy biography\nsecond line  '
        UserProfile.objects.filter(pk=profile.pk).update(
            nickname=legacy_nickname,
            bio=legacy_bio,
            updated_at=timezone.now(),
        )
        profile.refresh_from_db()

        response = self.client.post(
            reverse('profile_edit'),
            self.payload(
                bio=legacy_bio.replace('\n', '\r\n'),
                home_feed_mode=UserProfile.HomeFeedMode.PAGINATED,
            ),
        )

        self.assertRedirects(response, reverse('profile_edit'))
        profile.refresh_from_db()
        self.assertEqual(profile.nickname, legacy_nickname)
        self.assertEqual(profile.bio, legacy_bio)
        self.assertEqual(
            profile.home_feed_mode,
            UserProfile.HomeFeedMode.PAGINATED,
        )

    def test_changed_profile_text_keeps_existing_trim_behavior(self):
        profile = self.user.profile

        response = self.client.post(
            reverse('profile_edit'),
            self.payload(
                nickname='  new nickname  ',
                bio='  new biography  ',
            ),
        )

        self.assertRedirects(response, reverse('profile_edit'))
        profile.refresh_from_db()
        self.assertEqual(profile.nickname, 'new nickname')
        self.assertEqual(profile.bio, 'new biography')

    def test_new_overlong_bio_is_rejected_without_changing_legacy_value(self):
        profile = self.user.profile
        original_bio = 'legacy preserved'
        profile.bio = original_bio
        profile.save(update_fields=['bio', 'updated_at'])

        response = self.client.post(
            reverse('profile_edit'),
            self.payload(bio='x' * (PROFILE_BIO_MAX_LENGTH + 1)),
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('bio', response.context['form'].errors)
        profile.refresh_from_db()
        self.assertEqual(profile.bio, original_bio)


class UserProfileAdminGrandfatherTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='profile-admin-target')
        self.profile = self.user.profile
        self.legacy_bio = '旧' * (PROFILE_BIO_MAX_LENGTH + 25)
        UserProfile.objects.filter(pk=self.profile.pk).update(bio=self.legacy_bio)
        self.profile.refresh_from_db()

    def data(self, **overrides):
        values = {
            'user': self.user.pk,
            'version': self.profile.updated_at.isoformat(),
            'nickname': self.profile.nickname,
            'bio': self.profile.bio,
            'home_feed_mode': self.profile.home_feed_mode,
        }
        values.update(overrides)
        return values

    def test_admin_and_inline_use_the_grandfathering_form(self):
        self.assertIs(UserProfileAdmin.form, UserProfileAdminForm)
        self.assertIs(UserProfileInline.form, UserProfileAdminForm)

    def test_admin_change_pages_render_profile_versions(self):
        administrator = User.objects.create_superuser(
            username='profile-admin-viewer',
            email='admin@example.com',
            password='AdminViewPassword123!',
        )
        self.client.force_login(administrator)

        user_response = self.client.get(
            reverse('admin:auth_user_change', args=[self.user.pk]),
        )
        profile_response = self.client.get(
            reverse('admin:shares_userprofile_change', args=[self.profile.pk]),
        )

        self.assertEqual(user_response.status_code, 200)
        self.assertContains(user_response, 'name="profile-0-version"', html=False)
        self.assertEqual(profile_response.status_code, 200)
        self.assertContains(profile_response, 'name="version"', html=False)

    def test_admin_allows_unchanged_legacy_bio_while_editing_another_field(self):
        form = UserProfileAdminForm(
            data=self.data(nickname='admin nickname'),
            instance=self.profile,
        )

        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.nickname, 'admin nickname')
        self.assertEqual(saved.bio, self.legacy_bio)

    def test_admin_preserves_unchanged_legacy_whitespace(self):
        legacy_nickname = '  admin legacy nickname  '
        legacy_bio = '  admin legacy biography\nsecond line  '
        UserProfile.objects.filter(pk=self.profile.pk).update(
            nickname=legacy_nickname,
            bio=legacy_bio,
        )
        self.profile.refresh_from_db()

        form = UserProfileAdminForm(
            data=self.data(
                bio=legacy_bio.replace('\n', '\r\n'),
                home_feed_mode=UserProfile.HomeFeedMode.PAGINATED,
            ),
            instance=self.profile,
        )

        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.nickname, legacy_nickname)
        self.assertEqual(saved.bio, legacy_bio)

    def test_admin_rejects_a_new_overlong_bio(self):
        form = UserProfileAdminForm(
            data=self.data(bio='新' * (PROFILE_BIO_MAX_LENGTH + 1)),
            instance=self.profile,
        )

        self.assertFalse(form.is_valid())
        self.assertIn('bio', form.errors)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.bio, self.legacy_bio)

    def test_admin_rejects_a_stale_form_without_overwriting_newer_data(self):
        stale_version = self.profile.updated_at
        stale_bio = self.profile.bio
        newer_time = timezone.now() + timedelta(seconds=1)
        UserProfile.objects.filter(pk=self.profile.pk).update(
            bio='newer user biography',
            updated_at=newer_time,
        )
        self.profile.refresh_from_db()

        form = UserProfileAdminForm(
            data=self.data(
                version=stale_version.isoformat(),
                nickname='stale admin nickname',
                bio=stale_bio,
            ),
            instance=self.profile,
        )

        self.assertFalse(form.is_valid())
        self.assertIn('version', form.errors)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.nickname, '')
        self.assertEqual(self.profile.bio, 'newer user biography')
        self.assertEqual(self.profile.updated_at, newer_time)

    def test_admin_cannot_rebind_delete_or_create_profile_rows(self):
        other_user = User.objects.create_user(username='profile-admin-other')
        UserProfile.objects.filter(user=other_user).delete()
        form = UserProfileAdminForm(
            data=self.data(user=other_user.pk),
            instance=self.profile,
        )

        self.assertFalse(form.is_valid())
        self.assertIn('user', form.errors)

        profile_admin = UserProfileAdmin(UserProfile, AdminSite())
        request = RequestFactory().get('/admin/shares/userprofile/')
        self.assertFalse(profile_admin.has_add_permission(request))
        self.assertFalse(profile_admin.has_delete_permission(request, self.profile))
        self.assertIn('user', profile_admin.get_readonly_fields(request, self.profile))


class UserProfileAdminTransactionTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='profile-admin-lock-target')
        self.site = AdminSite()
        self.request = RequestFactory().post('/admin/profile/change/')

    def test_admin_change_views_hold_profile_lock_through_save_dispatch(self):
        atomic_states = []

        def user_changeform(*args, **kwargs):
            atomic_states.append(connection.in_atomic_block)
            return HttpResponse('user saved')

        def profile_changeform(*args, **kwargs):
            atomic_states.append(connection.in_atomic_block)
            return HttpResponse('profile saved')

        with patch.object(
            UserProfile.objects,
            'select_for_update',
            wraps=UserProfile.objects.select_for_update,
        ) as profile_lock:
            with patch.object(
                BaseUserAdmin,
                'changeform_view',
                side_effect=user_changeform,
            ):
                response = UserAdmin(User, self.site).changeform_view(
                    self.request,
                    str(self.user.pk),
                )
            self.assertEqual(response.content, b'user saved')

            with patch.object(
                ModelAdmin,
                'changeform_view',
                side_effect=profile_changeform,
            ):
                response = UserProfileAdmin(
                    UserProfile,
                    self.site,
                ).changeform_view(
                    self.request,
                    str(self.user.profile.pk),
                )
            self.assertEqual(response.content, b'profile saved')

        self.assertEqual(atomic_states, [True, True])
        self.assertEqual(profile_lock.call_count, 2)
        self.assertFalse(connection.in_atomic_block)
