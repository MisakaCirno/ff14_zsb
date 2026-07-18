from datetime import timedelta
from importlib import import_module

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class UserProfileIntegrityMigrationTests(TransactionTestCase):
    migrate_from = ('shares', '0022_add_share_restrictions')
    migrate_to = ('shares', '0023_userprofile_integrity')
    user_count = 1_005

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        self.fixture = self._create_fixture(old_apps)

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.apps = executor.loader.project_state([self.migrate_to]).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def _create_fixture(self, apps):
        User = apps.get_model('auth', 'User')
        UserProfile = apps.get_model('shares', 'UserProfile')
        joined_at = timezone.now() - timedelta(days=365)
        User.objects.bulk_create(
            [
                User(
                    username=f'profile-migration-{index:04d}',
                    date_joined=joined_at + timedelta(seconds=index),
                )
                for index in range(self.user_count)
            ],
            batch_size=1_000,
        )
        existing_user = User.objects.order_by('pk').first()
        existing_profile = UserProfile.objects.create(
            user_id=existing_user.pk,
            nickname='必须保留的昵称',
            bio='旧' * 1_200,
            home_feed_mode='paginated',
        )
        original_created_at = timezone.now() - timedelta(days=30)
        original_updated_at = timezone.now() - timedelta(days=7)
        UserProfile.objects.filter(pk=existing_profile.pk).update(
            created_at=original_created_at,
            updated_at=original_updated_at,
        )
        return {
            'existing_user_id': existing_user.pk,
            'existing_profile_id': existing_profile.pk,
            'original_created_at': original_created_at,
            'original_updated_at': original_updated_at,
        }

    def test_forward_is_lossless_batched_idempotent_and_reverse_is_noop(self):
        User = self.apps.get_model('auth', 'User')
        UserProfile = self.apps.get_model('shares', 'UserProfile')

        self.assertEqual(User.objects.count(), self.user_count)
        self.assertEqual(UserProfile.objects.count(), self.user_count)
        existing_profile = UserProfile.objects.get(
            pk=self.fixture['existing_profile_id'],
        )
        self.assertEqual(existing_profile.nickname, '必须保留的昵称')
        self.assertEqual(existing_profile.bio, '旧' * 1_200)
        self.assertEqual(existing_profile.home_feed_mode, 'paginated')
        self.assertEqual(
            existing_profile.created_at,
            self.fixture['original_created_at'],
        )
        self.assertEqual(
            existing_profile.updated_at,
            self.fixture['original_updated_at'],
        )

        backfilled = UserProfile.objects.exclude(pk=existing_profile.pk)
        self.assertEqual(backfilled.count(), self.user_count - 1)
        self.assertFalse(backfilled.exclude(nickname='').exists())
        self.assertFalse(backfilled.exclude(bio='').exists())
        self.assertFalse(backfilled.exclude(home_feed_mode='infinite').exists())
        self.assertFalse(backfilled.filter(created_at__isnull=True).exists())
        self.assertFalse(backfilled.filter(updated_at__isnull=True).exists())
        backfilled_timestamps = list(
            backfilled.order_by('user_id').values_list(
                'user_id',
                'created_at',
                'updated_at',
            )
        )
        expected_timestamps = [
            (user_id, date_joined, date_joined)
            for user_id, date_joined in User.objects.exclude(
                pk=self.fixture['existing_user_id'],
            ).order_by('pk').values_list('pk', 'date_joined')
        ]
        self.assertEqual(backfilled_timestamps, expected_timestamps)

        late_joined_at = timezone.now() - timedelta(days=2)
        late_user = User.objects.create(
            username='profile-migration-late-user',
            date_joined=late_joined_at,
        )
        migration = import_module('shares.migrations.0023_userprofile_integrity')

        class SchemaEditorStub:
            connection = connection

        migration.backfill_missing_user_profiles(self.apps, SchemaEditorStub())
        migration.backfill_missing_user_profiles(self.apps, SchemaEditorStub())
        self.assertEqual(
            UserProfile.objects.filter(user_id=late_user.pk).count(),
            1,
        )
        late_profile = UserProfile.objects.get(user_id=late_user.pk)
        self.assertEqual(late_profile.created_at, late_joined_at)
        self.assertEqual(late_profile.updated_at, late_joined_at)

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        reversed_apps = executor.loader.project_state([self.migrate_from]).apps
        ReversedProfile = reversed_apps.get_model('shares', 'UserProfile')
        self.assertEqual(ReversedProfile.objects.count(), self.user_count + 1)
        reversed_existing = ReversedProfile.objects.get(
            pk=self.fixture['existing_profile_id'],
        )
        self.assertEqual(reversed_existing.bio, '旧' * 1_200)
        self.assertEqual(reversed_existing.nickname, '必须保留的昵称')
