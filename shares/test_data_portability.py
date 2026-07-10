import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth.models import Group, Permission, User
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
    IMPORT_REPORT_FILENAME,
    MANIFEST_FILENAME,
    DataPortabilityError,
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
