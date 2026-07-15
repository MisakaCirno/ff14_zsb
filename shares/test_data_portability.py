import json
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth.models import Group, Permission, User
from django.core import serializers
from django.test import TestCase
from django.utils import timezone

from .models import (
    Announcement,
    Collection,
    CollectionItem,
    Report,
    Share,
    ShareLog,
    SiteMessage,
    UserProfile,
)
from .services.data_portability import (
    DATASET_FORMAT,
    DATASET_VERSION,
    ENTITY_SPECS,
    IMPORT_REPORT_FILENAME,
    MANIFEST_FILENAME,
    V1_ENTITY_FIELDS,
    DataPortabilityError,
    database_matches_manifest,
    export_dataset,
    import_dataset,
    validate_dataset,
)


class DataPortabilityTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(
            username='author',
            password='password123',
            email='author@example.com',
        )
        self.reporter = User.objects.create_user(
            username='reporter',
            password='password456',
        )
        permission = Permission.objects.order_by('pk').first()
        self.group = Group.objects.create(name='moderators')
        self.group.permissions.add(permission)
        self.author.groups.add(self.group)
        self.author.user_permissions.add(permission)
        self.author.profile.nickname = '作者昵称'
        self.author.profile.bio = '作者简介'
        self.author.profile.save()

        self.share = Share.objects.create(
            share_id='2a3b4c5d',
            title='迁移测试分享',
            strategy_code='[stgy:portable]',
            description='<p>迁移描述</p>',
            author=self.author,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
            views=12,
            copies=3,
        )
        self.share.likes.add(self.reporter)
        self.share.favorites.add(self.author)
        self.collection = Collection.objects.create(
            title='迁移测试合集',
            author=self.author,
        )
        CollectionItem.objects.create(
            collection=self.collection,
            share=self.share,
            order=1,
        )
        self.report = Report.objects.create(
            share=self.share,
            reporter=self.reporter,
            reason='历史举报',
            status=Report.Status.DISMISSED,
            resolved_at=timezone.now(),
            resolved_by=self.author,
            resolution_reason='已核查',
        )
        ShareLog.objects.create(
            share=self.share,
            user=self.author,
            action=ShareLog.ActionType.CREATE,
            details='迁移日志',
        )
        Announcement.objects.create(title='迁移公告', content='<p>公告</p>')
        SiteMessage.objects.create(
            recipient=self.reporter,
            sender=self.author,
            message_type=SiteMessage.MessageType.REPORT_DISMISSED,
            title='迁移消息',
            content='消息正文',
            related_share=self.share,
            related_report=self.report,
            metadata={'action_url': self.share.get_absolute_url()},
        )

    def export_to(self, directory: Path):
        dataset = directory / 'dataset'
        manifest = export_dataset(dataset)
        return dataset, manifest

    def downgrade_to_v1(self, dataset: Path) -> dict:
        manifest_path = dataset / MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['format_version'] = 1
        for spec in ENTITY_SPECS:
            data_path = dataset / spec.filename
            with data_path.open('w', encoding='utf-8', newline='\n') as stream:
                serializers.serialize(
                    'jsonl',
                    spec.model._default_manager.order_by(spec.model._meta.pk.name),
                    stream=stream,
                    use_natural_foreign_keys=True,
                    fields=V1_ENTITY_FIELDS[spec.name],
                )
            manifest['entities'][spec.name]['sha256'] = sha256(
                data_path.read_bytes()
            ).hexdigest()
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
            encoding='utf-8',
            newline='\n',
        )
        return manifest

    def test_version_1_schema_and_digest_ignore_future_model_fields(self):
        from .services import data_portability

        with TemporaryDirectory() as temporary:
            dataset, _ = self.export_to(Path(temporary))
            manifest = self.downgrade_to_v1(dataset)
            current_serialized_fields = data_portability._current_serialized_fields

            def fields_with_future_addition(spec):
                return {
                    *current_serialized_fields(spec),
                    f'future_{spec.name}_field',
                }

            with patch.object(
                data_portability,
                '_current_serialized_fields',
                side_effect=fields_with_future_addition,
            ):
                validation = validate_dataset(dataset)
                self.assertTrue(validation.valid, validation.as_dict())
                self.assertTrue(database_matches_manifest(manifest))

            self.assertEqual(
                set(V1_ENTITY_FIELDS),
                {spec.name for spec in ENTITY_SPECS},
            )
            self.assertNotIn('restriction_state', V1_ENTITY_FIELDS['shares'])

    def clear_portable_data(self):
        SiteMessage.objects.all().delete()
        ShareLog.objects.all().delete()
        Report.objects.all().delete()
        CollectionItem.objects.all().delete()
        Collection.objects.all().delete()
        Share.objects.all().delete()
        Announcement.objects.all().delete()
        UserProfile.objects.all().delete()
        User.objects.all().delete()
        Group.objects.all().delete()

    def test_export_writes_versioned_manifest_hashes_and_valid_report(self):
        with TemporaryDirectory() as temporary:
            dataset, manifest = self.export_to(Path(temporary))
            report = validate_dataset(dataset)

            self.assertEqual(manifest['format'], DATASET_FORMAT)
            self.assertEqual(manifest['format_version'], DATASET_VERSION)
            self.assertTrue((dataset / MANIFEST_FILENAME).is_file())
            self.assertTrue(report.valid)
            self.assertEqual(manifest['entities']['users']['count'], 2)
            self.assertEqual(manifest['entities']['shares']['count'], 1)
            for metadata in manifest['entities'].values():
                self.assertEqual(len(metadata['sha256']), 64)
                self.assertTrue((dataset / metadata['file']).is_file())

    def test_tampered_record_is_quarantined_and_blocks_validation(self):
        with TemporaryDirectory() as temporary:
            dataset, _ = self.export_to(Path(temporary))
            shares_path = dataset / 'shares.jsonl'
            record = json.loads(shares_path.read_text(encoding='utf-8').splitlines()[0])
            record['fields']['title'] = 'x' * 201
            shares_path.write_text(
                json.dumps(record, ensure_ascii=False) + '\n',
                encoding='utf-8',
                newline='\n',
            )

            report = validate_dataset(dataset)

            self.assertFalse(report.valid)
            self.assertIn('Checksum mismatch: shares.jsonl', report.errors)
            quarantined = [
                item
                for item in report.quarantined_records
                if item['entity'] == 'shares'
            ]
            self.assertEqual(len(quarantined), 1)
            self.assertIn('title exceeds max length 200', quarantined[0]['errors'])

    def test_import_round_trip_preserves_ids_passwords_and_relations(self):
        author_id = self.author.id
        password_hash = self.author.password
        share_pk = self.share.pk
        report_pk = self.report.pk
        restricted_at = timezone.now().replace(microsecond=123000)
        Share.objects.filter(pk=share_pk).update(
            restriction_state=Share.RestrictionState.REPORT_TAKEDOWN,
            restriction_reason='管理员确认下架',
            restricted_at=restricted_at,
            restricted_by=self.author,
        )
        with TemporaryDirectory() as temporary:
            dataset, _ = self.export_to(Path(temporary))
            self.clear_portable_data()

            status = import_dataset(dataset)

            self.assertEqual(status, 'imported')
            author = User.objects.get(pk=author_id)
            share = Share.objects.get(pk=share_pk)
            report = Report.objects.get(pk=report_pk)
            self.assertEqual(author.password, password_hash)
            self.assertEqual(author.profile.nickname, '作者昵称')
            self.assertTrue(author.groups.filter(name='moderators').exists())
            self.assertEqual(share.share_id, '2a3b4c5d')
            self.assertEqual(list(share.likes.values_list('username', flat=True)), ['reporter'])
            self.assertEqual(list(share.favorites.values_list('username', flat=True)), ['author'])
            self.assertEqual(
                share.restriction_state,
                Share.RestrictionState.REPORT_TAKEDOWN,
            )
            self.assertEqual(share.restriction_reason, '管理员确认下架')
            self.assertEqual(share.restricted_at, restricted_at)
            self.assertEqual(share.restricted_by, author)
            self.assertEqual(report.resolution_reason, '已核查')
            self.assertEqual(SiteMessage.objects.get().related_report_id, report_pk)

            new_user = User.objects.create_user(username='after-import')
            new_share = Share.objects.create(
                title='导入后新分享',
                strategy_code='[stgy:after-import]',
                author=new_user,
            )
            self.assertGreater(new_user.pk, max(author_id, self.reporter.pk))
            self.assertGreater(new_share.pk, share_pk)

            new_share.delete()
            new_user.delete()
            self.assertEqual(import_dataset(dataset), 'already_imported')

    def test_version_1_import_preserves_legacy_data_and_derives_restrictions(self):
        base_time = (timezone.now() - timedelta(days=5)).replace(microsecond=0)
        Report.objects.filter(pk=self.report.pk).update(
            status=Report.Status.RESOLVED,
            resolved_at=base_time,
            resolved_by=self.author,
            resolution_reason='  管理员确认违规  ',
        )
        approval_after_report = ShareLog.objects.create(
            share=self.share,
            user=self.author,
            action=ShareLog.ActionType.REVIEW_APPROVE,
        )
        ShareLog.objects.filter(pk=approval_after_report.pk).update(
            created_at=base_time + timedelta(days=4)
        )

        currently_rejected = Share.objects.create(
            title='当前审核拒绝',
            strategy_code='[stgy:current-rejected]',
            author=self.author,
            status=Share.Status.REJECTED,
            visibility=Share.Visibility.PRIVATE,
            review_feedback='  审核元数据原因  ',
            reviewed_at=base_time + timedelta(days=1),
            reviewed_by=self.author,
            restriction_state=Share.RestrictionState.REVIEW_REJECTED,
            restriction_reason='审核元数据原因',
            restricted_at=base_time + timedelta(days=1),
            restricted_by=self.author,
        )
        current_reject_log = ShareLog.objects.create(
            share=currently_rejected,
            user=self.reporter,
            action=ShareLog.ActionType.REVIEW_REJECT,
            details='日志原因不应覆盖审核元数据',
        )
        ShareLog.objects.filter(pk=current_reject_log.pk).update(created_at=base_time)

        historically_rejected = Share.objects.create(
            title='历史拒绝后被作者编辑',
            strategy_code='[stgy:historical-rejected]',
            author=self.author,
            status=Share.Status.APPROVED,
        )
        old_approval = ShareLog.objects.create(
            share=historically_rejected,
            user=self.author,
            action=ShareLog.ActionType.REVIEW_APPROVE,
        )
        newest_reject = ShareLog.objects.create(
            share=historically_rejected,
            user=self.reporter,
            action=ShareLog.ActionType.REVIEW_REJECT,
            details='  历史拒绝原因  ',
        )
        # Equal timestamps use the later primary key, matching the DB migration.
        ShareLog.objects.filter(pk=old_approval.pk).update(
            created_at=base_time + timedelta(days=2)
        )
        ShareLog.objects.filter(pk=newest_reject.pk).update(
            created_at=base_time + timedelta(days=2)
        )

        historically_cleared = Share.objects.create(
            title='拒绝后经管理员通过',
            strategy_code='[stgy:historical-cleared]',
            author=self.author,
            status=Share.Status.APPROVED,
        )
        old_reject = ShareLog.objects.create(
            share=historically_cleared,
            user=self.reporter,
            action=ShareLog.ActionType.REVIEW_REJECT,
            details='已解除的旧原因',
        )
        newest_approval = ShareLog.objects.create(
            share=historically_cleared,
            user=self.author,
            action=ShareLog.ActionType.REVIEW_APPROVE,
        )
        # Here the approval has the later primary key, so the restriction is clear.
        ShareLog.objects.filter(pk=old_reject.pk).update(
            created_at=base_time + timedelta(days=3)
        )
        ShareLog.objects.filter(pk=newest_approval.pk).update(
            created_at=base_time + timedelta(days=3)
        )

        legacy_private = Share.objects.create(
            title='旧版私密来源待确认',
            strategy_code='[stgy:legacy-private-review]',
            author=self.author,
            status=Share.Status.APPROVED,
            visibility=Share.Visibility.PRIVATE,
            restriction_state=Share.RestrictionState.LEGACY_PRIVATE,
            restriction_reason='历史私密状态来源待人工确认',
            restricted_at=base_time + timedelta(days=4),
        )
        legacy_private_updated_at = legacy_private.updated_at.replace(
            microsecond=(legacy_private.updated_at.microsecond // 1000) * 1000,
        )

        with TemporaryDirectory() as temporary:
            dataset, _ = self.export_to(Path(temporary))
            manifest = self.downgrade_to_v1(dataset)
            validation = validate_dataset(dataset)

            self.assertTrue(validation.valid, validation.as_dict())
            self.assertEqual(validation.as_dict()['format_version'], 1)
            self.assertTrue(validation.warnings)

            share_pk = self.share.pk
            current_pk = currently_rejected.pk
            historical_pk = historically_rejected.pk
            cleared_pk = historically_cleared.pk
            legacy_private_pk = legacy_private.pk
            self.clear_portable_data()

            self.assertEqual(import_dataset(dataset), 'imported')
            report_restricted = Share.objects.get(pk=share_pk)
            current_restricted = Share.objects.get(pk=current_pk)
            historical_restricted = Share.objects.get(pk=historical_pk)
            cleared = Share.objects.get(pk=cleared_pk)
            imported_legacy_private = Share.objects.get(pk=legacy_private_pk)

            self.assertEqual(
                report_restricted.restriction_state,
                Share.RestrictionState.REPORT_TAKEDOWN,
            )
            self.assertEqual(report_restricted.restriction_reason, '管理员确认违规')
            self.assertEqual(report_restricted.restricted_at, base_time)
            self.assertEqual(report_restricted.restricted_by.username, 'author')
            self.assertEqual(
                current_restricted.restriction_state,
                Share.RestrictionState.REVIEW_REJECTED,
            )
            self.assertEqual(current_restricted.restriction_reason, '审核元数据原因')
            self.assertEqual(
                current_restricted.restricted_at,
                base_time + timedelta(days=1),
            )
            self.assertEqual(current_restricted.restricted_by.username, 'author')
            self.assertEqual(
                historical_restricted.restriction_state,
                Share.RestrictionState.REVIEW_REJECTED,
            )
            self.assertEqual(historical_restricted.restriction_reason, '历史拒绝原因')
            self.assertEqual(
                historical_restricted.restricted_at,
                base_time + timedelta(days=2),
            )
            self.assertEqual(historical_restricted.restricted_by.username, 'reporter')
            self.assertEqual(cleared.restriction_state, Share.RestrictionState.CLEAR)
            self.assertEqual(cleared.restriction_reason, '')
            self.assertIsNone(cleared.restricted_at)
            self.assertIsNone(cleared.restricted_by)
            self.assertEqual(
                imported_legacy_private.restriction_state,
                Share.RestrictionState.LEGACY_PRIVATE,
            )
            self.assertEqual(
                imported_legacy_private.restriction_reason,
                '历史私密状态来源待人工确认',
            )
            self.assertEqual(
                imported_legacy_private.restricted_at,
                legacy_private_updated_at,
            )
            self.assertIsNone(imported_legacy_private.restricted_by)

            self.assertEqual(
                list(report_restricted.likes.values_list('username', flat=True)),
                ['reporter'],
            )
            self.assertEqual(
                list(report_restricted.favorites.values_list('username', flat=True)),
                ['author'],
            )
            self.assertEqual(
                Report.objects.get(pk=self.report.pk).resolution_reason,
                '  管理员确认违规  ',
            )
            self.assertTrue(database_matches_manifest(manifest))
            self.assertEqual(import_dataset(dataset), 'already_imported')

            Share.objects.filter(pk=share_pk).update(restriction_reason='被篡改')
            self.assertFalse(database_matches_manifest(manifest))
            with self.assertRaises(DataPortabilityError):
                import_dataset(dataset)

    def test_version_1_rejects_deleted_audit_actors_but_version_2_allows_them(self):
        ShareLog.objects.filter(share=self.share).update(user=None)
        Report.objects.filter(pk=self.report.pk).update(reporter=None)
        with TemporaryDirectory() as temporary:
            dataset, _ = self.export_to(Path(temporary))

            self.assertTrue(validate_dataset(dataset).valid)
            self.downgrade_to_v1(dataset)
            report = validate_dataset(dataset)

            self.assertFalse(report.valid)
            share_log_errors = [
                item['errors']
                for item in report.quarantined_records
                if item['entity'] == 'share_logs'
            ]
            self.assertTrue(share_log_errors)
            self.assertIn('version 1 share log has no user', share_log_errors[0])
            report_errors = [
                item['errors']
                for item in report.quarantined_records
                if item['entity'] == 'reports'
            ]
            self.assertTrue(report_errors)
            self.assertIn('version 1 report has no reporter', report_errors[0])

    def test_version_2_round_trip_preserves_multiple_reports_with_deleted_reporters(self):
        reporters = [
            User.objects.create_user(username=f'deleted-reporter-{index}')
            for index in range(2)
        ]
        pending_reports = [
            Report.objects.create(
                share=self.share,
                reporter=reporter,
                reason=f'待处理举报 {index}',
            )
            for index, reporter in enumerate(reporters)
        ]
        report_ids = {report.pk for report in pending_reports}
        for reporter in reporters:
            reporter.delete()

        self.assertEqual(
            Report.objects.filter(pk__in=report_ids, reporter=None).count(),
            2,
        )
        with TemporaryDirectory() as temporary:
            dataset, _ = self.export_to(Path(temporary))
            validation = validate_dataset(dataset)
            self.assertTrue(validation.valid, validation.as_dict())
            self.clear_portable_data()

            self.assertEqual(import_dataset(dataset), 'imported')

        self.assertEqual(
            Report.objects.filter(pk__in=report_ids, reporter=None).count(),
            2,
        )

    def test_version_1_uses_safe_fallback_restriction_reasons(self):
        restricted_at = timezone.now().replace(microsecond=0)
        report_share = Share.objects.create(
            title='缺少举报处理说明',
            strategy_code='[stgy:missing-report-resolution]',
            author=self.author,
            restriction_state=Share.RestrictionState.REPORT_TAKEDOWN,
            restriction_reason='历史举报下架记录未保存处理说明',
            restricted_at=restricted_at,
        )
        Report.objects.create(
            share=report_share,
            reporter=self.reporter,
            reason='用户举报原文不可作为管理员处理依据',
            status=Report.Status.RESOLVED,
            resolved_at=restricted_at,
            resolution_reason='',
        )
        rejected_share = Share.objects.create(
            title='缺少审核拒绝原因',
            strategy_code='[stgy:missing-review-reason]',
            author=self.author,
            status=Share.Status.REJECTED,
            visibility=Share.Visibility.PRIVATE,
            restriction_state=Share.RestrictionState.REVIEW_REJECTED,
            restriction_reason='历史审核拒绝记录未保存原因',
            restricted_at=restricted_at,
        )

        with TemporaryDirectory() as temporary:
            dataset, _ = self.export_to(Path(temporary))
            self.downgrade_to_v1(dataset)
            report_pk = report_share.pk
            rejected_pk = rejected_share.pk
            self.clear_portable_data()

            self.assertEqual(import_dataset(dataset), 'imported')
            imported_report_share = Share.objects.get(pk=report_pk)
            imported_rejected_share = Share.objects.get(pk=rejected_pk)
            self.assertEqual(
                imported_report_share.restriction_reason,
                '历史举报下架记录未保存处理说明',
            )
            self.assertNotEqual(
                imported_report_share.restriction_reason,
                '用户举报原文不可作为管理员处理依据',
            )
            self.assertEqual(
                imported_rejected_share.restriction_reason,
                '历史审核拒绝记录未保存原因',
            )
            self.assertIsNotNone(imported_rejected_share.restricted_at)

    def test_import_error_rolls_back_all_portable_rows_and_writes_report(self):
        with TemporaryDirectory() as temporary:
            dataset, _ = self.export_to(Path(temporary))
            self.clear_portable_data()
            from django.core import serializers as django_serializers

            real_deserialize = django_serializers.deserialize
            call_count = 0

            def flaky_deserialize(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 3:
                    raise ValueError('injected import failure')
                return real_deserialize(*args, **kwargs)

            with patch(
                'shares.services.data_portability.serializers.deserialize',
                side_effect=flaky_deserialize,
            ):
                with self.assertRaises(DataPortabilityError):
                    import_dataset(dataset)

            self.assertFalse(User.objects.exists())
            self.assertFalse(Group.objects.exists())
            self.assertFalse(Share.objects.exists())
            self.assertFalse(Announcement.objects.exists())
            report_payload = json.loads(
                (dataset / IMPORT_REPORT_FILENAME).read_text(encoding='utf-8')
            )
            self.assertFalse(report_payload['valid'])
            self.assertTrue(report_payload['quarantined_records'])
