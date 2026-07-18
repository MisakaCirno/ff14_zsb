from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import TransactionTestCase
from django.utils import timezone


class DataIntegrityConstraintMigrationTests(TransactionTestCase):
    migrate_from = ('shares', '0020_replace_ckeditor_field')
    migrate_to = ('shares', '0021_add_data_integrity_constraints')

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        self.addCleanup(self._restore_leaf_migrations)
        self.apps = executor.loader.project_state([self.migrate_from]).apps

    @staticmethod
    def _restore_leaf_migrations():
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    @staticmethod
    def _model_snapshot(model):
        field_names = [field.attname for field in model._meta.concrete_fields]
        return list(model.objects.order_by('pk').values(*field_names))

    def _snapshot_fixture(self, apps):
        model_names = (
            'UserProfile',
            'Share',
            'Report',
            'Collection',
            'CollectionItem',
            'ShareLog',
            'Announcement',
            'SiteMessage',
        )
        snapshot = {
            name: self._model_snapshot(apps.get_model('shares', name))
            for name in model_names
        }
        Share = apps.get_model('shares', 'Share')
        snapshot['Share_favorites'] = self._model_snapshot(
            Share._meta.get_field('favorites').remote_field.through,
        )
        snapshot['Share_likes'] = self._model_snapshot(
            Share._meta.get_field('likes').remote_field.through,
        )
        return snapshot

    def _create_valid_fixture(self):
        User = self.apps.get_model('auth', 'User')
        UserProfile = self.apps.get_model('shares', 'UserProfile')
        Share = self.apps.get_model('shares', 'Share')
        Report = self.apps.get_model('shares', 'Report')
        Collection = self.apps.get_model('shares', 'Collection')
        CollectionItem = self.apps.get_model('shares', 'CollectionItem')
        ShareLog = self.apps.get_model('shares', 'ShareLog')
        Announcement = self.apps.get_model('shares', 'Announcement')
        SiteMessage = self.apps.get_model('shares', 'SiteMessage')

        author = User.objects.create(username='integrity-migration-author')
        reporter = User.objects.create(username='integrity-migration-reporter')
        moderator = User.objects.create(username='integrity-migration-moderator')
        UserProfile.objects.create(
            user_id=author.pk,
            nickname='约束迁移保留资料',
            bio='Unicode 与长文本必须完整保留：' + ('数据' * 200),
            home_feed_mode='paginated',
        )

        reviewed_at = timezone.now()
        approved = Share.objects.create(
            share_id='valid001',
            title='已审核分享',
            strategy_code='[stgy:integrity-approved]',
            description='完整保留的分享正文',
            author_id=author.pk,
            category='combat',
            visibility='unlisted',
            status='approved',
            review_feedback='审核字段不能被表重建改写',
            reviewed_at=reviewed_at,
            reviewed_by_id=moderator.pk,
            views=17,
            copies=9,
            is_nsfw=True,
            is_spoiler=True,
            is_original=True,
        )
        pending = Share.objects.create(
            share_id='valid002',
            title='待审核分享',
            strategy_code='[stgy:integrity-pending]',
            description='待审核记录保持空审核元数据',
            author_id=author.pk,
            category='entertainment',
            visibility='private',
            status='pending',
            views=0,
            copies=0,
        )
        approved.favorites.add(reporter)
        approved.likes.add(moderator)

        Report.objects.create(
            share_id=approved.pk,
            reporter_id=reporter.pk,
            reason='仍待处理的举报',
            status='pending',
        )
        resolved_report = Report.objects.create(
            share_id=pending.pk,
            reporter_id=reporter.pk,
            reason='已经处理的举报',
            status='resolved',
            resolved_at=reviewed_at,
            resolved_by_id=moderator.pk,
            resolution_reason='处理说明逐字保留',
        )

        collection = Collection.objects.create(
            author_id=author.pk,
            title='迁移测试合集',
            description='合集说明完整保留',
            is_public=False,
        )
        CollectionItem.objects.create(
            collection_id=collection.pk,
            share_id=approved.pk,
            order=0,
        )
        CollectionItem.objects.create(
            collection_id=collection.pk,
            share_id=pending.pk,
            order=1,
        )
        ShareLog.objects.create(
            share_id=approved.pk,
            user_id=moderator.pk,
            action='approve',
            details='审核日志不能丢失',
        )
        Announcement.objects.create(
            title='公告标题',
            content='<p>公告 <strong>HTML</strong> 与 Unicode 内容</p>',
            is_active=True,
        )
        SiteMessage.objects.create(
            recipient_id=author.pk,
            sender_id=moderator.pk,
            message_type='report_resolved',
            title='站内信标题',
            content='站内信正文完整保留',
            metadata={'source': 'migration-test', 'nested': {'value': 1}},
            related_share_id=pending.pk,
            related_report_id=resolved_report.pk,
        )

    def test_valid_rows_survive_all_sqlite_table_rebuilds_verbatim(self):
        self._create_valid_fixture()
        expected = self._snapshot_fixture(self.apps)

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        migrated_apps = executor.loader.project_state([self.migrate_to]).apps

        self.assertEqual(self._snapshot_fixture(migrated_apps), expected)
        MigratedShare = migrated_apps.get_model('shares', 'Share')
        self.assertEqual(
            MigratedShare._meta.get_field('share_id').max_length,
            21,
        )
        with connection.cursor() as cursor:
            share_constraints = connection.introspection.get_constraints(
                cursor,
                MigratedShare._meta.db_table,
            )
            report_constraints = connection.introspection.get_constraints(
                cursor,
                migrated_apps.get_model('shares', 'Report')._meta.db_table,
            )
            profile_constraints = connection.introspection.get_constraints(
                cursor,
                migrated_apps.get_model('shares', 'UserProfile')._meta.db_table,
            )
            item_constraints = connection.introspection.get_constraints(
                cursor,
                migrated_apps.get_model('shares', 'CollectionItem')._meta.db_table,
            )
        self.assertTrue(
            {
                'share_category_valid',
                'share_visibility_valid',
                'share_status_valid',
                'share_views_nonnegative',
                'share_copies_nonnegative',
                'share_pending_unreviewed',
                'share_reviewer_has_time',
            }
            <= set(share_constraints)
        )
        self.assertTrue(
            {'collection_share_unique', 'collection_order_unique'}
            <= set(item_constraints)
        )
        self.assertTrue(
            {
                'report_status_valid',
                'report_pending_unresolved',
                'report_finished_has_time',
                'report_one_pending',
            }
            <= set(report_constraints)
        )
        self.assertIn('profile_feed_mode_valid', profile_constraints)

    def test_invalid_rows_fail_before_schema_changes_and_remain_unchanged(self):
        User = self.apps.get_model('auth', 'User')
        UserProfile = self.apps.get_model('shares', 'UserProfile')
        Share = self.apps.get_model('shares', 'Share')
        Collection = self.apps.get_model('shares', 'Collection')
        CollectionItem = self.apps.get_model('shares', 'CollectionItem')

        user = User.objects.create(username='integrity-invalid-user')
        invalid_profile = UserProfile.objects.create(
            user_id=user.pk,
            nickname='保持不变的非法来源记录',
            bio='迁移必须先失败，不能猜测修复',
            home_feed_mode='unsupported-mode',
        )
        first_share = Share.objects.create(
            share_id='badslot1',
            title='重复排序一',
            strategy_code='[stgy:bad-slot-1]',
            author_id=user.pk,
        )
        second_share = Share.objects.create(
            share_id='badslot2',
            title='重复排序二',
            strategy_code='[stgy:bad-slot-2]',
            author_id=user.pk,
        )
        collection = Collection.objects.create(
            author_id=user.pk,
            title='重复排序合集',
        )
        first_item = CollectionItem.objects.create(
            collection_id=collection.pk,
            share_id=first_share.pk,
            order=7,
        )
        second_item = CollectionItem.objects.create(
            collection_id=collection.pk,
            share_id=second_share.pk,
            order=7,
        )
        expected = self._snapshot_fixture(self.apps)

        def repair_for_leaf_cleanup():
            UserProfile.objects.filter(pk=invalid_profile.pk).update(
                home_feed_mode='paginated',
            )
            CollectionItem.objects.filter(pk=second_item.pk).update(order=8)

        self.addCleanup(repair_for_leaf_cleanup)

        executor = MigrationExecutor(connection)
        with self.assertRaisesRegex(
            RuntimeError,
            'invalid profile feed mode: 1.*duplicate collection order slots: 1',
        ):
            executor.migrate([self.migrate_to])

        self.assertFalse(
            MigrationRecorder.Migration.objects.filter(
                app='shares',
                name=self.migrate_to[1],
            ).exists(),
        )
        self.assertEqual(self._snapshot_fixture(self.apps), expected)
        self.assertEqual(
            CollectionItem.objects.get(pk=first_item.pk).order,
            CollectionItem.objects.get(pk=second_item.pk).order,
        )
        with connection.cursor() as cursor:
            profile_constraints = connection.introspection.get_constraints(
                cursor,
                UserProfile._meta.db_table,
            )
            item_constraints = connection.introspection.get_constraints(
                cursor,
                CollectionItem._meta.db_table,
            )
        self.assertNotIn('profile_feed_mode_valid', profile_constraints)
        self.assertNotIn('collection_order_unique', item_constraints)
