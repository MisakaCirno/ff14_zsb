from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, tag


@tag('slow')
class HomeFeedModeDefaultMigrationTests(TransactionTestCase):
    migrate_from = ('shares', '0017_userprofile_home_feed_mode')
    migrate_to = ('shares', '0018_default_home_feed_waterfall')

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        User = old_apps.get_model('auth', 'User')
        UserProfile = old_apps.get_model('shares', 'UserProfile')

        paginated_user = User.objects.create(username='migration-paginated-user')
        infinite_user = User.objects.create(username='migration-infinite-user')
        self.paginated_profile_id = UserProfile.objects.create(
            user_id=paginated_user.pk,
            nickname='分页偏好用户',
            bio='必须逐字段保留的分页资料',
            home_feed_mode='paginated',
        ).pk
        self.infinite_profile_id = UserProfile.objects.create(
            user_id=infinite_user.pk,
            nickname='瀑布偏好用户',
            bio='必须逐字段保留的瀑布资料',
            home_feed_mode='infinite',
        ).pk
        self.expected_profiles = {
            item['pk']: item
            for item in UserProfile.objects.order_by('pk').values(
                'pk',
                'user_id',
                'nickname',
                'bio',
                'home_feed_mode',
                'created_at',
                'updated_at',
            )
        }

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.apps = executor.loader.project_state([self.migrate_to]).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def profile_snapshot(self, UserProfile):
        return {
            item['pk']: item
            for item in UserProfile.objects.order_by('pk').values(
                'pk',
                'user_id',
                'nickname',
                'bio',
                'home_feed_mode',
                'created_at',
                'updated_at',
            )
        }

    def test_default_change_preserves_existing_profiles_and_uses_new_default(self):
        User = self.apps.get_model('auth', 'User')
        UserProfile = self.apps.get_model('shares', 'UserProfile')

        self.assertEqual(self.profile_snapshot(UserProfile), self.expected_profiles)
        self.assertEqual(
            UserProfile._meta.get_field('home_feed_mode').get_default(),
            'infinite',
        )

        new_user = User.objects.create(username='migration-new-default-user')
        new_profile = UserProfile.objects.create(user_id=new_user.pk)
        self.assertEqual(new_profile.home_feed_mode, 'infinite')

    def test_reverse_default_change_does_not_rewrite_existing_preferences(self):
        User = self.apps.get_model('auth', 'User')
        UserProfile = self.apps.get_model('shares', 'UserProfile')
        new_user = User.objects.create(username='migration-reverse-default-user')
        new_profile = UserProfile.objects.create(user_id=new_user.pk)
        expected_profiles = self.profile_snapshot(UserProfile)

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        reversed_apps = executor.loader.project_state([self.migrate_from]).apps
        ReversedProfile = reversed_apps.get_model('shares', 'UserProfile')

        self.assertEqual(self.profile_snapshot(ReversedProfile), expected_profiles)
        self.assertEqual(
            ReversedProfile.objects.get(pk=new_profile.pk).home_feed_mode,
            'infinite',
        )
        self.assertEqual(
            ReversedProfile._meta.get_field('home_feed_mode').get_default(),
            'paginated',
        )


@tag('slow')
class LegacyHomeFeedModeMigrationTests(TransactionTestCase):
    migrate_from = ('shares', '0016_add_copies_field')
    migrate_to = ('shares', '0018_default_home_feed_waterfall')

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        User = old_apps.get_model('auth', 'User')
        UserProfile = old_apps.get_model('shares', 'UserProfile')
        existing_user = User.objects.create(username='migration-legacy-user')
        existing_profile = UserProfile.objects.create(
            user_id=existing_user.pk,
            nickname='旧版用户',
            bio='升级前没有浏览模式字段',
        )
        self.existing_profile_id = existing_profile.pk
        self.expected_legacy_fields = {
            field: getattr(existing_profile, field)
            for field in (
                'pk',
                'user_id',
                'nickname',
                'bio',
                'created_at',
                'updated_at',
            )
        }

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.apps = executor.loader.project_state([self.migrate_to]).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_legacy_profiles_keep_paginated_behavior_while_new_profiles_use_infinite(self):
        User = self.apps.get_model('auth', 'User')
        UserProfile = self.apps.get_model('shares', 'UserProfile')
        existing_profile = UserProfile.objects.get(pk=self.existing_profile_id)

        actual_legacy_fields = {
            field: getattr(existing_profile, field)
            for field in self.expected_legacy_fields
        }
        self.assertEqual(actual_legacy_fields, self.expected_legacy_fields)
        self.assertEqual(existing_profile.home_feed_mode, 'paginated')

        new_user = User.objects.create(username='migration-post-default-user')
        new_profile = UserProfile.objects.create(user_id=new_user.pk)
        self.assertEqual(new_profile.home_feed_mode, 'infinite')


@tag('slow')
class AppliedHomeFeedModeMigrationTests(TransactionTestCase):
    migrate_from = ('shares', '0018_default_home_feed_waterfall')
    migrate_to = ('shares', '0028_normalize_announcement_column_order')

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        User = old_apps.get_model('auth', 'User')
        UserProfile = old_apps.get_model('shares', 'UserProfile')

        for mode in ('paginated', 'infinite'):
            user = User.objects.create(username=f'already-applied-{mode}-user')
            UserProfile.objects.create(
                user_id=user.pk,
                nickname=f'already-applied-{mode}',
                bio=f'profile already stored after migration 0018: {mode}',
                home_feed_mode=mode,
            )
        self.expected_profiles = list(
            UserProfile.objects.order_by('pk').values(
                'pk',
                'user_id',
                'nickname',
                'bio',
                'home_feed_mode',
                'created_at',
                'updated_at',
            )
        )

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.apps = executor.loader.project_state([self.migrate_to]).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_later_migrations_preserve_preferences_when_0018_is_already_applied(self):
        UserProfile = self.apps.get_model('shares', 'UserProfile')

        actual_profiles = list(
            UserProfile.objects.order_by('pk').values(
                'pk',
                'user_id',
                'nickname',
                'bio',
                'home_feed_mode',
                'created_at',
                'updated_at',
            )
        )
        self.assertEqual(actual_profiles, self.expected_profiles)
