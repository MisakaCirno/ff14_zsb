from django.db import IntegrityError, connection, transaction
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
        Report = self.apps.get_model('shares', 'Report')
        Collection = self.apps.get_model('shares', 'Collection')
        CollectionItem = self.apps.get_model('shares', 'CollectionItem')

        user = User.objects.create(username='integrity-invalid-user')
        moderator = User.objects.create(username='integrity-invalid-moderator')
        reporter = User.objects.create(username='integrity-invalid-reporter')
        now = timezone.now()
        invalid_profile = UserProfile.objects.create(
            user_id=user.pk,
            nickname='保持不变的非法来源记录',
            bio='迁移必须先失败，不能猜测修复',
            home_feed_mode='unsupported-mode',
        )
        invalid_enums = Share.objects.create(
            share_id='bad-enums',
            title='invalid enum and counters',
            strategy_code='[stgy:bad-enums]',
            author_id=user.pk,
            category='unknown-category',
            visibility='unknown-visibility',
            status='unknown-status',
            views=-1,
            copies=-2,
        )
        pending_with_review = Share.objects.create(
            share_id='bad-pending-review',
            title='pending with review metadata',
            strategy_code='[stgy:bad-pending-review]',
            author_id=user.pk,
            status='pending',
            reviewed_at=now,
            reviewed_by_id=moderator.pk,
        )
        reviewer_without_time = Share.objects.create(
            share_id='bad-reviewer-time',
            title='reviewer without review time',
            strategy_code='[stgy:bad-reviewer-time]',
            author_id=user.pk,
            status='approved',
            reviewed_by_id=moderator.pk,
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
        invalid_status_report = Report.objects.create(
            share_id=invalid_enums.pk,
            reporter_id=reporter.pk,
            reason='invalid report status',
            status='unknown-status',
            resolved_at=now,
        )
        pending_with_resolution = Report.objects.create(
            share_id=pending_with_review.pk,
            reporter_id=reporter.pk,
            reason='pending report with resolution data',
            status='pending',
            resolved_at=now,
            resolved_by_id=moderator.pk,
            resolution_reason='must remain unchanged after failed preflight',
        )
        finished_without_time = Report.objects.create(
            share_id=reviewer_without_time.pk,
            reporter_id=reporter.pk,
            reason='finished report missing resolution time',
            status='dismissed',
        )
        Report.objects.create(
            share_id=first_share.pk,
            reporter_id=reporter.pk,
            reason='duplicate pending report one',
            status='pending',
        )
        duplicate_pending_two = Report.objects.create(
            share_id=first_share.pk,
            reporter_id=reporter.pk,
            reason='duplicate pending report two',
            status='pending',
        )
        expected = self._snapshot_fixture(self.apps)

        def repair_for_leaf_cleanup():
            UserProfile.objects.filter(pk=invalid_profile.pk).update(
                home_feed_mode='paginated',
            )
            Share.objects.filter(pk=invalid_enums.pk).update(
                category='combat',
                visibility='public',
                status='approved',
                views=0,
                copies=0,
            )
            Share.objects.filter(pk=pending_with_review.pk).update(
                reviewed_at=None,
                reviewed_by_id=None,
            )
            Share.objects.filter(pk=reviewer_without_time.pk).update(
                reviewed_at=now,
            )
            Report.objects.filter(pk=invalid_status_report.pk).update(
                status='dismissed',
            )
            Report.objects.filter(pk=pending_with_resolution.pk).update(
                resolved_at=None,
                resolved_by_id=None,
                resolution_reason='',
            )
            Report.objects.filter(pk=finished_without_time.pk).update(
                resolved_at=now,
            )
            Report.objects.filter(pk=duplicate_pending_two.pk).update(
                status='dismissed',
                resolved_at=now,
            )
            CollectionItem.objects.filter(pk=second_item.pk).update(order=8)

        self.addCleanup(repair_for_leaf_cleanup)

        executor = MigrationExecutor(connection)
        with self.assertRaises(RuntimeError) as raised:
            executor.migrate([self.migrate_to])

        message = str(raised.exception)
        for violation in (
            'invalid profile feed mode: 1',
            'invalid share category: 1',
            'invalid share visibility: 1',
            'invalid share status: 1',
            'negative share counters: 1',
            'pending shares with review metadata: 1',
            'reviewer without review time: 1',
            'invalid report status: 1',
            'pending reports with resolution data: 1',
            'finished reports without resolution time: 1',
            'duplicate pending reports: 1',
            'duplicate collection order slots: 1',
        ):
            with self.subTest(violation=violation):
                self.assertIn(violation, message)

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

    def test_database_constraints_reject_each_invalid_write_atomically(self):
        self._create_valid_fixture()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        apps = executor.loader.project_state([self.migrate_to]).apps

        UserProfile = apps.get_model('shares', 'UserProfile')
        Share = apps.get_model('shares', 'Share')
        Report = apps.get_model('shares', 'Report')
        CollectionItem = apps.get_model('shares', 'CollectionItem')
        author_profile = UserProfile.objects.get(
            user__username='integrity-migration-author',
        )
        approved = Share.objects.get(share_id='valid001')
        pending = Share.objects.get(share_id='valid002')
        pending_report = Report.objects.get(status='pending')
        resolved_report = Report.objects.get(status='resolved')
        first_item = CollectionItem.objects.get(order=0)
        collection_id = first_item.collection_id
        extra_share = Share.objects.create(
            share_id='valid003',
            title='constraint-only extra share',
            strategy_code='[stgy:constraint-only]',
            author_id=approved.author_id,
            category='combat',
            visibility='public',
            status='approved',
        )
        expected = self._snapshot_fixture(apps)

        invalid_writes = {
            'profile feed mode': lambda: UserProfile.objects.filter(
                pk=author_profile.pk,
            ).update(home_feed_mode='unsupported-mode'),
            'share category': lambda: Share.objects.filter(pk=approved.pk).update(
                category='unsupported-category',
            ),
            'share visibility': lambda: Share.objects.filter(pk=approved.pk).update(
                visibility='unsupported-visibility',
            ),
            'share status': lambda: Share.objects.filter(pk=approved.pk).update(
                status='unsupported-status',
            ),
            'negative views': lambda: Share.objects.filter(pk=approved.pk).update(
                views=-1,
            ),
            'negative copies': lambda: Share.objects.filter(pk=approved.pk).update(
                copies=-1,
            ),
            'pending review metadata': lambda: Share.objects.filter(
                pk=pending.pk,
            ).update(reviewed_at=timezone.now()),
            'reviewer without review time': lambda: Share.objects.filter(
                pk=approved.pk,
            ).update(reviewed_at=None),
            'report status': lambda: Report.objects.filter(
                pk=resolved_report.pk,
            ).update(status='unsupported-status'),
            'pending report resolution data': lambda: Report.objects.filter(
                pk=pending_report.pk,
            ).update(resolution_reason='not allowed while pending'),
            'finished report without time': lambda: Report.objects.filter(
                pk=resolved_report.pk,
            ).update(resolved_at=None),
            'duplicate pending report': lambda: Report.objects.create(
                share_id=pending_report.share_id,
                reporter_id=pending_report.reporter_id,
                reason='second pending report',
                status='pending',
            ),
            'duplicate collection share': lambda: CollectionItem.objects.create(
                collection_id=collection_id,
                share_id=first_item.share_id,
                order=99,
            ),
            'duplicate collection order': lambda: CollectionItem.objects.create(
                collection_id=collection_id,
                share_id=extra_share.pk,
                order=first_item.order,
            ),
        }
        for label, invalid_write in invalid_writes.items():
            with self.subTest(constraint=label):
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        invalid_write()

        self.assertEqual(self._snapshot_fixture(apps), expected)
