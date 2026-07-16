import json
import sqlite3
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.admin.models import ADDITION, LogEntry
from django.contrib.auth.models import Group, Permission, User
from django.contrib.contenttypes.models import ContentType
from django.contrib.sessions.models import Session
from django.core import serializers
from django.db import DatabaseError, connection, models, transaction
from django.db.backends.sqlite3.base import DatabaseWrapper as SQLiteDatabaseWrapper
from django.test import TestCase, TransactionTestCase
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
    ENTITY_SPECS_BY_VERSION,
    IMPORT_REPORT_FILENAME,
    MANIFEST_FILENAME,
    V1_ENTITY_FIELDS,
    V2_ENTITY_FIELDS,
    V3_ENTITY_FIELDS,
    DataPortabilityError,
    database_matches_manifest,
    export_dataset,
    import_dataset as _import_dataset,
    validate_dataset,
)


HISTORICAL_DATASET_ROOT = Path(__file__).resolve().parent / 'testdata'
HISTORICAL_MANIFEST_SHA256 = {
    1: '7a1781550aa1defb39ef69e9940a83c81f4c2ce83af2945c2f47ed484e620065',
    2: 'bc1371246e6af67e80a3d312f2eb8bc591850613758a4897306a2ec621b799ce',
}


def import_dataset(*args, **kwargs):
    kwargs.setdefault('confirm_exclusive_target', True)
    return _import_dataset(*args, **kwargs)


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

    def downgrade_dataset(self, dataset: Path, *, version: int, fields_by_entity) -> dict:
        manifest_path = dataset / MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['format_version'] = version
        target_specs = ENTITY_SPECS_BY_VERSION[version]
        target_names = {spec.name for spec in target_specs}
        for entity_name in set(manifest['entities']) - target_names:
            metadata = manifest['entities'].pop(entity_name)
            (dataset / metadata['file']).unlink(missing_ok=True)
        for key in (
            'codec',
            'schema_fingerprint',
            'dependencies',
            'migration_projection',
            'identity',
            'table_projection',
            'session_projection',
        ):
            manifest.pop(key, None)
        for spec in target_specs:
            data_path = dataset / spec.filename
            with data_path.open('w', encoding='utf-8', newline='\n') as stream:
                serializers.serialize(
                    'jsonl',
                    spec.model._default_manager.order_by(spec.model._meta.pk.name),
                    stream=stream,
                    use_natural_foreign_keys=True,
                    fields=fields_by_entity[spec.name],
                )
            manifest['entities'][spec.name]['count'] = spec.model._default_manager.count()
            manifest['entities'][spec.name]['sha256'] = sha256(
                data_path.read_bytes()
            ).hexdigest()
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
            encoding='utf-8',
            newline='\n',
        )
        return manifest

    def downgrade_to_v1(self, dataset: Path) -> dict:
        return self.downgrade_dataset(
            dataset,
            version=1,
            fields_by_entity=V1_ENTITY_FIELDS,
        )

    def downgrade_to_v2(self, dataset: Path) -> dict:
        return self.downgrade_dataset(
            dataset,
            version=2,
            fields_by_entity=V2_ENTITY_FIELDS,
        )

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
                {spec.name for spec in ENTITY_SPECS_BY_VERSION[1]},
            )
            self.assertNotIn('restriction_state', V1_ENTITY_FIELDS['shares'])

    def test_version_2_schema_and_digest_ignore_future_model_fields(self):
        from .services import data_portability

        with TemporaryDirectory() as temporary:
            dataset, _ = self.export_to(Path(temporary))
            manifest = self.downgrade_to_v2(dataset)
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

    def test_frozen_version_2_fields_cover_its_historical_entities(self):
        self.assertEqual(
            set(V2_ENTITY_FIELDS),
            {spec.name for spec in ENTITY_SPECS_BY_VERSION[2]},
        )
        self.assertEqual(
            set(V2_ENTITY_FIELDS['shares']) - set(V1_ENTITY_FIELDS['shares']),
            {
                'restriction_state',
                'restriction_reason',
                'restricted_at',
                'restricted_by',
            },
        )

    def test_frozen_version_3_fields_match_current_models(self):
        from .services import data_portability

        if DATASET_VERSION != 3:
            self.skipTest('v3 is historical; validate it through its versioned adapter')
        self.assertEqual(
            set(V3_ENTITY_FIELDS),
            {spec.name for spec in ENTITY_SPECS_BY_VERSION[3]},
        )
        for spec in ENTITY_SPECS_BY_VERSION[3]:
            self.assertEqual(
                set(V3_ENTITY_FIELDS[spec.name]),
                data_portability._current_serialized_fields(spec),
                f'{spec.name} changed without a dataset version bump',
            )

    def test_v3_semantic_schema_and_reference_protocol_are_frozen(self):
        from .services import data_portability

        self.assertEqual(
            data_portability._current_v3_model_schema_signature(),
            '9b91a3b943d2986115508db51c216d94040053ec2c8e19b900acd2e0ddfdd685',
        )
        self.assertEqual(
            data_portability._schema_fingerprint(3),
            '5748cb65c7617cef02e2141435c80530b6736b1bd4c5ab91419772a374ad55c2',
        )
        self.assertEqual(
            data_portability._internal_database_tables(
                {'sqlite_sequence', 'sqlite_customer_data'},
                vendor='sqlite',
            ),
            {'sqlite_sequence'},
        )
        self.assertEqual(
            data_portability._internal_database_tables(
                {'sqlite_sequence', 'sqlite_customer_data'},
                vendor='postgresql',
            ),
            set(),
        )

        with patch.object(
            User,
            'natural_key',
            lambda user: (user.username, user.email),
        ):
            with TemporaryDirectory() as temporary:
                dataset, _ = self.export_to(Path(temporary))
                profiles = [
                    json.loads(line)
                    for line in (dataset / 'user_profiles.jsonl').read_text(
                        encoding='utf-8'
                    ).splitlines()
                ]

        author_profile = next(
            record
            for record in profiles
            if record['fields']['nickname'] == self.author.profile.nickname
        )
        self.assertEqual(author_profile['fields']['user'], ['author'])

    def clear_portable_data(self):
        Session.objects.all().delete()
        LogEntry.objects.all().delete()
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

    def historical_dataset(self, version: int) -> Path:
        return HISTORICAL_DATASET_ROOT / f'data_portability_v{version}'

    def assert_historical_fixture_import(self, version: int):
        dataset = self.historical_dataset(version)
        immutable_bytes = {
            path.name: path.read_bytes()
            for path in dataset.iterdir()
            if path.is_file()
        }
        manifest = json.loads(immutable_bytes[MANIFEST_FILENAME])
        share_payload = json.loads(immutable_bytes['shares.jsonl'])
        report_payload = json.loads(immutable_bytes['reports.jsonl'])

        if version == 1:
            self.assertNotIn(
                'restriction_state',
                share_payload['fields'],
            )
            self.assertEqual(
                report_payload['fields']['resolution_reason'],
                '  固定历史举报下架  ',
            )
        else:
            self.assertEqual(
                share_payload['fields']['restriction_state'],
                Share.RestrictionState.REPORT_TAKEDOWN,
            )
            self.assertEqual(
                share_payload['fields']['restriction_reason'],
                'v2 固定持久化限制原因',
            )

        self.clear_portable_data()
        with TemporaryDirectory() as temporary:
            report_path = Path(temporary) / f'v{version}-import-report.json'
            self.assertEqual(
                import_dataset(dataset, report_path=report_path),
                'imported',
            )
            import_report = json.loads(report_path.read_text(encoding='utf-8'))
            self.assertEqual(import_report['format_version'], version)
            self.assertEqual(import_report['status'], 'imported')
            self.assertEqual(
                import_report['manifest_sha256'],
                HISTORICAL_MANIFEST_SHA256[version],
            )

            self.assertTrue(database_matches_manifest(manifest))
            self.assertEqual(
                import_dataset(dataset, report_path=report_path),
                'already_imported',
            )

        expected_counts = {
            spec.name: 1
            for spec in ENTITY_SPECS_BY_VERSION[version]
        }
        self.assertEqual(
            {
                spec.name: spec.model._default_manager.count()
                for spec in ENTITY_SPECS_BY_VERSION[version]
            },
            expected_counts,
        )
        owner = User.objects.get(pk=101)
        self.assertEqual(owner.username, 'fixture-owner')
        self.assertEqual(owner.password, '!fixture-unusable-password')
        self.assertEqual(
            list(owner.groups.values_list('name', flat=True)),
            ['fixture-moderators'],
        )
        self.assertEqual(
            list(owner.user_permissions.values_list(
                'codename',
                'content_type__app_label',
                'content_type__model',
            )),
            [('change_share', 'shares', 'share')],
        )
        fixture_group = Group.objects.get(name='fixture-moderators')
        self.assertEqual(
            list(fixture_group.permissions.values_list(
                'codename',
                'content_type__app_label',
                'content_type__model',
            )),
            [('view_share', 'shares', 'share')],
        )
        self.assertEqual(owner.profile.nickname, '固定样本用户')

        share = Share.objects.get(pk=201)
        self.assertEqual(share.share_id, '2a3b4c5d')
        self.assertEqual(
            share.restriction_state,
            Share.RestrictionState.REPORT_TAKEDOWN,
        )
        expected_reason = (
            '固定历史举报下架'
            if version == 1
            else 'v2 固定持久化限制原因'
        )
        expected_restricted_at = (
            datetime(2024, 1, 4, 1, 2, 3, 456000, tzinfo=UTC)
            if version == 1
            else datetime(2024, 1, 5, 6, 7, 8, 901000, tzinfo=UTC)
        )
        self.assertEqual(share.restriction_reason, expected_reason)
        self.assertEqual(
            share.restricted_at,
            expected_restricted_at,
        )
        self.assertEqual(share.restricted_by, owner)
        self.assertEqual(
            list(share.likes.values_list('username', flat=True)),
            ['fixture-owner'],
        )
        self.assertEqual(
            list(share.favorites.values_list('username', flat=True)),
            ['fixture-owner'],
        )
        self.assertEqual(
            CollectionItem.objects.get(pk=401).share,
            share,
        )
        imported_report = Report.objects.get(pk=501)
        self.assertEqual(imported_report.share, share)
        self.assertEqual(
            imported_report.resolution_reason,
            '  固定历史举报下架  ',
        )
        message = SiteMessage.objects.get(pk=801)
        self.assertEqual(message.related_share, share)
        self.assertEqual(message.related_report, imported_report)
        self.assertEqual(message.metadata['fixture'], 'v1-v2')

        self.assertEqual(
            {
                path.name: path.read_bytes()
                for path in dataset.iterdir()
                if path.is_file()
            },
            immutable_bytes,
        )

    def test_historical_v1_and_v2_golden_fixtures_are_fixed_and_valid(self):
        for version in (1, 2):
            with self.subTest(version=version):
                dataset = self.historical_dataset(version)
                manifest_path = dataset / MANIFEST_FILENAME
                self.assertEqual(
                    sha256(manifest_path.read_bytes()).hexdigest(),
                    HISTORICAL_MANIFEST_SHA256[version],
                )
                manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
                specs = ENTITY_SPECS_BY_VERSION[version]
                self.assertEqual(manifest['format'], DATASET_FORMAT)
                self.assertEqual(manifest['format_version'], version)
                self.assertEqual(
                    set(manifest['entities']),
                    {spec.name for spec in specs},
                )
                self.assertEqual(
                    {path.name for path in dataset.iterdir()},
                    {MANIFEST_FILENAME, *(spec.filename for spec in specs)},
                )
                for metadata in manifest['entities'].values():
                    self.assertEqual(metadata['count'], 1)
                    self.assertEqual(
                        sha256((dataset / metadata['file']).read_bytes()).hexdigest(),
                        metadata['sha256'],
                    )

                validation = validate_dataset(dataset)
                self.assertTrue(validation.valid, validation.as_dict())
                self.assertEqual(
                    validation.entity_counts,
                    {spec.name: 1 for spec in specs},
                )
                self.assertEqual(bool(validation.warnings), version == 1)

    def test_historical_v1_golden_fixture_imports_and_derives_restriction(self):
        self.assert_historical_fixture_import(1)

    def test_historical_v2_golden_fixture_imports_persisted_restriction(self):
        self.assert_historical_fixture_import(2)

    def test_export_writes_versioned_manifest_hashes_and_valid_report(self):
        with TemporaryDirectory() as temporary:
            dataset, manifest = self.export_to(Path(temporary))
            report = validate_dataset(dataset)

            self.assertEqual(manifest['format'], DATASET_FORMAT)
            self.assertEqual(manifest['format_version'], DATASET_VERSION)
            self.assertEqual(len(manifest['schema_fingerprint']), 64)
            self.assertTrue((dataset / MANIFEST_FILENAME).is_file())
            self.assertTrue(report.valid)
            self.assertEqual(manifest['entities']['users']['count'], 2)
            self.assertEqual(manifest['entities']['shares']['count'], 1)
            projection = manifest['table_projection']
            self.assertEqual(
                set(projection['direct']),
                {spec.model._meta.db_table for spec in ENTITY_SPECS_BY_VERSION[3]},
            )
            self.assertIn(Share.likes.through._meta.db_table, projection['embedded'])
            self.assertIn(
                ContentType._meta.db_table,
                projection['regenerated'],
            )
            self.assertIn(
                Permission._meta.db_table,
                projection['regenerated'],
            )
            self.assertEqual(projection['unknown_nonempty'], {})
            permission = Permission.objects.order_by('pk').first()
            self.assertIn(
                {
                    'natural_key': list(permission.natural_key()),
                    'name': permission.name,
                },
                manifest['dependencies']['permissions'],
            )
            self.assertIn(
                list(permission.content_type.natural_key()),
                manifest['dependencies']['content_types'],
            )
            self.assertIn(
                list(permission.natural_key()),
                manifest['dependencies']['references']['permissions'],
            )
            migration_projection = manifest['migration_projection']
            applied_nodes = {
                (item['app'], item['name'])
                for item in migration_projection['applied']
            }
            self.assertTrue(migration_projection['leaf_nodes'])
            self.assertTrue(
                {
                    tuple(node)
                    for node in migration_projection['leaf_nodes']
                }.issubset(applied_nodes)
            )
            for item in migration_projection['applied']:
                self.assertRegex(
                    item['applied_at'],
                    r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$',
                )
            for metadata in manifest['entities'].values():
                self.assertEqual(len(metadata['sha256']), 64)
                self.assertTrue((dataset / metadata['file']).is_file())

    def test_v3_preserves_six_digit_microseconds_and_sorts_embedded_relations(self):
        exact_time = datetime(2026, 7, 16, 1, 2, 3, 123456, tzinfo=UTC)
        Share.objects.filter(pk=self.share.pk).update(
            restricted_at=exact_time,
            restricted_by=self.author,
            restriction_state=Share.RestrictionState.REPORT_TAKEDOWN,
            restriction_reason='精确时间测试',
            updated_at=exact_time,
        )
        UserProfile.objects.filter(user=self.author).update(updated_at=exact_time)
        z_user = User.objects.create_user(username='z-user')
        a_user = User.objects.create_user(username='a-user')
        self.share.likes.add(z_user, a_user)
        z_group = Group.objects.create(name='z-group')
        a_group = Group.objects.create(name='a-group')
        self.author.groups.add(z_group, a_group)
        SiteMessage.objects.filter(pk=SiteMessage.objects.get().pk).update(
            metadata={'z': {'second': 2, 'first': 1}, 'a': ['值']},
        )

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_dataset, first_manifest = self.export_to(root / 'first')
            second_dataset, second_manifest = self.export_to(root / 'second')

            self.assertEqual(
                first_manifest['entities']['shares']['sha256'],
                second_manifest['entities']['shares']['sha256'],
            )
            share_record = json.loads(
                (first_dataset / 'shares.jsonl').read_text(encoding='utf-8').splitlines()[0]
            )
            self.assertEqual(
                share_record['fields']['restricted_at'],
                '2026-07-16T01:02:03.123456Z',
            )
            self.assertEqual(
                share_record['fields']['likes'],
                [['a-user'], ['reporter'], ['z-user']],
            )
            user_records = [
                json.loads(line)
                for line in (first_dataset / 'users.jsonl').read_text(
                    encoding='utf-8',
                ).splitlines()
            ]
            author_record = next(
                record
                for record in user_records
                if record['fields']['username'] == 'author'
            )
            self.assertEqual(
                author_record['fields']['groups'],
                [['a-group'], ['moderators'], ['z-group']],
            )
            profile_records = [
                json.loads(line)
                for line in (first_dataset / 'user_profiles.jsonl').read_text(
                    encoding='utf-8',
                ).splitlines()
            ]
            author_profile = next(
                record
                for record in profile_records
                if record['fields']['user'] == ['author']
            )
            self.assertEqual(
                author_profile['fields']['updated_at'],
                '2026-07-16T01:02:03.123456Z',
            )
            message_line = (first_dataset / 'site_messages.jsonl').read_text(
                encoding='utf-8',
            )
            self.assertIn(
                '"metadata":{"a":["值"],"z":{"first":1,"second":2}}',
                message_line,
            )

            self.clear_portable_data()
            self.assertEqual(import_dataset(first_dataset), 'imported')

        imported = Share.objects.get(pk=self.share.pk)
        self.assertEqual(imported.restricted_at, exact_time)
        self.assertEqual(imported.updated_at, exact_time)
        self.assertEqual(
            User.objects.get(username='author').profile.updated_at,
            exact_time,
        )

    def test_v3_round_trip_preserves_admin_log_and_content_type_natural_key(self):
        exact_time = datetime(2026, 7, 16, 4, 5, 6, 654321, tzinfo=UTC)
        content_type = ContentType.objects.get_for_model(Share)
        entry = LogEntry.objects.create(
            user=self.author,
            content_type=content_type,
            object_id=str(self.share.pk),
            object_repr='迁移测试分享',
            action_flag=ADDITION,
            change_message='由管理后台创建',
        )
        LogEntry.objects.filter(pk=entry.pk).update(action_time=exact_time)

        with TemporaryDirectory() as temporary:
            dataset, _ = self.export_to(Path(temporary))
            record = json.loads(
                (dataset / 'admin_log_entries.jsonl').read_text(
                    encoding='utf-8',
                ).strip()
            )
            self.assertEqual(record['fields']['user'], ['author'])
            self.assertEqual(record['fields']['content_type'], ['shares', 'share'])
            self.assertEqual(
                record['fields']['action_time'],
                '2026-07-16T04:05:06.654321Z',
            )
            self.clear_portable_data()
            self.assertEqual(import_dataset(dataset), 'imported')

        imported = LogEntry.objects.get(pk=entry.pk)
        self.assertEqual(imported.user.username, 'author')
        self.assertEqual(imported.content_type.natural_key(), ('shares', 'share'))
        self.assertEqual(imported.action_time, exact_time)
        self.assertEqual(imported.change_message, '由管理后台创建')

    def test_v3_records_session_logout_projection_without_session_payload(self):
        expiry = (
            timezone.now().astimezone(UTC) + timedelta(days=30)
        ).replace(microsecond=987654)
        Session.objects.create(
            session_key='portable-session-key',
            session_data='opaque-session-payload-must-not-be-exported',
            expire_date=expiry,
        )

        with TemporaryDirectory() as temporary:
            dataset, manifest = self.export_to(Path(temporary))
            projection = manifest['session_projection']
            self.assertEqual(projection['policy'], 'force_logout_at_cutover')
            self.assertEqual(projection['source_row_count'], 1)
            self.assertEqual(projection['source_unexpired_count'], 1)
            self.assertEqual(
                projection['source_latest_expiry'],
                expiry.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
            )
            self.assertEqual(projection['target_required_row_count'], 0)
            self.assertEqual(
                manifest['table_projection']['excluded']['django_session'],
                projection,
            )
            self.assertNotIn('sessions', manifest['entities'])
            self.assertNotIn(
                'opaque-session-payload-must-not-be-exported',
                json.dumps(manifest, ensure_ascii=False),
            )
            self.assertFalse((dataset / 'sessions.jsonl').exists())

            self.clear_portable_data()
            self.assertEqual(import_dataset(dataset), 'imported')
            self.assertFalse(Session.objects.exists())

    def test_v3_admin_log_with_unmappable_content_type_is_quarantined(self):
        LogEntry.objects.create(
            user=self.author,
            content_type=ContentType.objects.get_for_model(Share),
            object_id=str(self.share.pk),
            object_repr='待隔离日志',
            action_flag=ADDITION,
        )
        with TemporaryDirectory() as temporary:
            dataset, _ = self.export_to(Path(temporary))
            data_path = dataset / 'admin_log_entries.jsonl'
            record = json.loads(data_path.read_text(encoding='utf-8'))
            record['fields']['content_type'] = ['removed_app', 'removed_model']
            data_path.write_text(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    separators=(',', ':'),
                    sort_keys=True,
                ) + '\n',
                encoding='utf-8',
                newline='\n',
            )
            manifest_path = dataset / MANIFEST_FILENAME
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            manifest['entities']['admin_log_entries']['sha256'] = sha256(
                data_path.read_bytes()
            ).hexdigest()
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                encoding='utf-8',
                newline='\n',
            )

            report = validate_dataset(dataset)

        self.assertFalse(report.valid)
        errors = next(
            item['errors']
            for item in report.quarantined_records
            if item['entity'] == 'admin_log_entries'
        )
        self.assertIn(
            'admin log content type cannot be mapped on this target',
            errors,
        )

    def test_v3_group_with_unmappable_permission_is_quarantined(self):
        with TemporaryDirectory() as temporary:
            dataset, _ = self.export_to(Path(temporary))
            data_path = dataset / 'groups.jsonl'
            record = json.loads(data_path.read_text(encoding='utf-8'))
            record['fields']['permissions'] = [
                ['missing_permission', 'removed_app', 'removed_model'],
            ]
            data_path.write_text(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    separators=(',', ':'),
                    sort_keys=True,
                ) + '\n',
                encoding='utf-8',
                newline='\n',
            )
            manifest_path = dataset / MANIFEST_FILENAME
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            manifest['entities']['groups']['sha256'] = sha256(
                data_path.read_bytes()
            ).hexdigest()
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                encoding='utf-8',
                newline='\n',
            )

            report = validate_dataset(dataset)

        self.assertFalse(report.valid)
        errors = next(
            item['errors']
            for item in report.quarantined_records
            if item['entity'] == 'groups'
        )
        self.assertIn(
            "unknown permission reference: "
            "['missing_permission', 'removed_app', 'removed_model']",
            errors,
        )

    def test_v3_fails_closed_for_unreferenced_regenerated_dependencies(self):
        stale_content_type = ContentType.objects.create(
            app_label='retired_plugin',
            model='retired_record',
        )
        stale_permission = Permission.objects.create(
            content_type=stale_content_type,
            codename='view_retired_record',
            name='Can view retired record',
        )
        natural_key = list(stale_permission.natural_key())

        with TemporaryDirectory() as temporary:
            dataset, manifest = self.export_to(Path(temporary))
            self.assertIn(
                ['retired_plugin', 'retired_record'],
                manifest['dependencies']['content_types'],
            )
            self.assertIn(
                {
                    'natural_key': natural_key,
                    'name': 'Can view retired record',
                },
                manifest['dependencies']['permissions'],
            )
            self.assertNotIn(
                natural_key,
                manifest['dependencies']['references']['permissions'],
            )

            Permission.objects.filter(pk=stale_permission.pk).update(
                name='Renamed on target',
            )
            renamed_report = validate_dataset(dataset)
            self.assertFalse(renamed_report.valid)
            self.assertIn(
                'Target Permission names differ from source: '
                'view_retired_record/retired_plugin/retired_record',
                renamed_report.errors,
            )

            stale_content_type.delete()
            missing_report = validate_dataset(dataset)

        self.assertFalse(missing_report.valid)
        self.assertIn(
            'Target cannot map source ContentTypes: retired_plugin.retired_record',
            missing_report.errors,
        )
        self.assertIn(
            'Target cannot map source Permissions: '
            'view_retired_record/retired_plugin/retired_record',
            missing_report.errors,
        )

    def test_v3_requires_source_migration_projection(self):
        with TemporaryDirectory() as temporary:
            dataset, _ = self.export_to(Path(temporary))
            manifest_path = dataset / MANIFEST_FILENAME
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            manifest.pop('migration_projection')
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                encoding='utf-8',
                newline='\n',
            )

            report = validate_dataset(dataset)

        self.assertFalse(report.valid)
        self.assertIn('v3 migration_projection must be an object', report.errors)

    def test_v3_accepts_a_source_migration_leaf_that_is_an_applied_ancestor(self):
        with TemporaryDirectory() as temporary:
            dataset, _ = self.export_to(Path(temporary))
            manifest_path = dataset / MANIFEST_FILENAME
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            leaves = manifest['migration_projection']['leaf_nodes']
            current_share_leaf = next(
                node for node in leaves if node[0] == 'shares'
            )
            self.assertEqual(current_share_leaf[1], '0025_add_collection_owner_index')
            leaves.remove(current_share_leaf)
            leaves.append(['shares', '0024_widen_site_message_titles'])
            leaves.sort()
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                encoding='utf-8',
                newline='\n',
            )

            report = validate_dataset(dataset)

        self.assertTrue(report.valid, report.as_dict())

    def test_v3_preserves_a_sequence_floor_above_the_highest_live_pk(self):
        if connection.vendor not in {'sqlite', 'postgresql'}:
            self.skipTest('v3 sequence floors support SQLite and PostgreSQL')

        deleted_share = Share.objects.create(
            title='已删除的高序列分享',
            strategy_code='[stgy:deleted-sequence-marker]',
            author=self.author,
        )
        deleted_pk = deleted_share.pk
        deleted_share.delete()

        with TemporaryDirectory() as temporary:
            dataset, manifest = self.export_to(Path(temporary))
            sequence = manifest['identity']['sequences']['shares']
            self.assertEqual(
                sequence['max_live_pk'],
                Share.objects.order_by('-pk').values_list('pk', flat=True).first(),
            )
            self.assertGreaterEqual(sequence['next_value_floor'], deleted_pk + 1)
            required_floor = sequence['next_value_floor']

            self.clear_portable_data()
            share_table = Share._meta.db_table
            if connection.vendor == 'sqlite':
                with connection.cursor() as cursor:
                    cursor.execute(
                        'UPDATE sqlite_sequence SET seq = 0 WHERE name = %s',
                        [share_table],
                    )
            else:
                with connection.cursor() as cursor:
                    cursor.execute(
                        'SELECT pg_get_serial_sequence(%s, %s)',
                        [share_table, Share._meta.pk.column],
                    )
                    sequence_name = cursor.fetchone()[0]
                    cursor.execute(
                        'SELECT setval(%s::regclass, %s, false)',
                        [sequence_name, 1],
                    )

            self.assertEqual(import_dataset(dataset), 'imported')
            imported_author = User.objects.get(username='author')
            new_share = Share.objects.create(
                title='序列恢复后的分享',
                strategy_code='[stgy:after-v3-sequence-restore]',
                author=imported_author,
            )

        self.assertGreaterEqual(new_share.pk, required_floor)

    def test_version_2_import_does_not_lower_a_sequence_high_water_mark(self):
        deleted_share = Share.objects.create(
            title='旧格式已删除的高序列分享',
            strategy_code='[stgy:v2-deleted-sequence-marker]',
            author=self.author,
        )
        deleted_pk = deleted_share.pk
        deleted_share.delete()
        live_max_pk = Share.objects.order_by('-pk').values_list('pk', flat=True).first()

        with TemporaryDirectory() as temporary:
            dataset, _ = self.export_to(Path(temporary))
            self.downgrade_to_v2(dataset)
            self.clear_portable_data()

            self.assertEqual(import_dataset(dataset), 'imported')
            imported_author = User.objects.get(username='author')
            new_share = Share.objects.create(
                title='旧格式序列重置后的分享',
                strategy_code='[stgy:after-v2-sequence-reset]',
                author=imported_author,
            )

        self.assertGreater(new_share.pk, deleted_pk)
        self.assertGreater(new_share.pk, live_max_pk)

    def test_v3_unknown_tables_are_only_allowed_while_empty(self):
        table_name = 'r19_unknown_projection'
        quoted_table = connection.ops.quote_name(table_name)
        with connection.cursor() as cursor:
            cursor.execute(
                f'CREATE TABLE {quoted_table} '
                '(id INTEGER PRIMARY KEY, marker VARCHAR(20))'
            )
        try:
            with TemporaryDirectory() as temporary:
                _, manifest = self.export_to(Path(temporary))
                self.assertIn(
                    table_name,
                    manifest['table_projection']['unknown_empty'],
                )

            with connection.cursor() as cursor:
                cursor.execute(
                    f'INSERT INTO {quoted_table} (id, marker) VALUES (%s, %s)',
                    [1, 'must-fail-closed'],
                )
            with TemporaryDirectory() as temporary:
                with self.assertRaisesRegex(
                    DataPortabilityError,
                    'Unknown non-empty database tables',
                ):
                    self.export_to(Path(temporary))
        finally:
            with connection.cursor() as cursor:
                cursor.execute(f'DROP TABLE {quoted_table}')

    def test_v3_fails_closed_for_unclassified_database_object_types(self):
        from .services import data_portability

        discovered = data_portability._discovered_database_objects()
        with patch.object(
            data_portability,
            '_discovered_database_objects',
            return_value={**discovered, 'external_business_data': 'm'},
        ):
            with TemporaryDirectory() as temporary:
                with self.assertRaisesRegex(
                    DataPortabilityError,
                    'Unclassified database objects',
                ):
                    self.export_to(Path(temporary))

    def test_v3_unknown_nonempty_target_table_blocks_import_and_exact_match(self):
        table_name = 'r19_unknown_target_data'
        quoted_table = connection.ops.quote_name(table_name)
        with TemporaryDirectory() as temporary:
            dataset, manifest = self.export_to(Path(temporary))
            self.clear_portable_data()
            with connection.cursor() as cursor:
                cursor.execute(
                    f'CREATE TABLE {quoted_table} '
                    '(id INTEGER PRIMARY KEY, marker VARCHAR(20))'
                )
                cursor.execute(
                    f'INSERT INTO {quoted_table} (id, marker) VALUES (%s, %s)',
                    [1, 'must-block-import'],
                )
            try:
                self.assertFalse(database_matches_manifest(manifest))
                with self.assertRaisesRegex(
                    DataPortabilityError,
                    'non-empty target database',
                ):
                    import_dataset(dataset)
                self.assertFalse(User.objects.exists())
                self.assertFalse(Share.objects.exists())
            finally:
                with connection.cursor() as cursor:
                    cursor.execute(f'DROP TABLE {quoted_table}')

    def test_version_2_manifest_rejects_a_v3_only_entity(self):
        with TemporaryDirectory() as temporary:
            dataset, _ = self.export_to(Path(temporary))
            manifest = self.downgrade_to_v2(dataset)
            data_path = dataset / 'admin_log_entries.jsonl'
            data_path.write_text('', encoding='utf-8', newline='\n')
            manifest['entities']['admin_log_entries'] = {
                'model': 'admin.logentry',
                'file': data_path.name,
                'count': 0,
                'sha256': sha256(data_path.read_bytes()).hexdigest(),
            }
            (dataset / MANIFEST_FILENAME).write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                encoding='utf-8',
                newline='\n',
            )

            report = validate_dataset(dataset)

        self.assertFalse(report.valid)
        self.assertIn(
            'Unexpected manifest entities: admin_log_entries',
            report.errors,
        )

    def test_old_dataset_does_not_match_a_target_with_admin_logs_or_sessions(self):
        with TemporaryDirectory() as temporary:
            dataset, _ = self.export_to(Path(temporary))
            manifest = self.downgrade_to_v2(dataset)
            self.clear_portable_data()
            self.assertEqual(import_dataset(dataset), 'imported')

            imported_user = User.objects.get(username='author')
            imported_share = Share.objects.get(pk=self.share.pk)
            LogEntry.objects.create(
                user=imported_user,
                content_type=ContentType.objects.get_for_model(Share),
                object_id=str(imported_share.pk),
                object_repr=imported_share.title,
                action_flag=ADDITION,
            )
            self.assertFalse(database_matches_manifest(manifest))
            with self.assertRaises(DataPortabilityError):
                import_dataset(dataset)

            LogEntry.objects.all().delete()
            self.assertTrue(database_matches_manifest(manifest))
            Session.objects.create(
                session_key='residual-target-session',
                session_data='must-block-old-dataset-match',
                expire_date=timezone.now() + timedelta(days=1),
            )
            self.assertFalse(database_matches_manifest(manifest))
            with self.assertRaises(DataPortabilityError):
                import_dataset(dataset)

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

    def test_dataset_missing_user_profile_is_rejected(self):
        with TemporaryDirectory() as temporary:
            dataset, _ = self.export_to(Path(temporary))
            profiles_path = dataset / 'user_profiles.jsonl'
            profile_lines = profiles_path.read_text(encoding='utf-8').splitlines()
            kept_lines = [
                line
                for line in profile_lines
                if json.loads(line)['fields']['user'] != ['reporter']
            ]
            self.assertEqual(len(kept_lines), len(profile_lines) - 1)
            profiles_path.write_text(
                ''.join(f'{line}\n' for line in kept_lines),
                encoding='utf-8',
                newline='\n',
            )

            manifest_path = dataset / MANIFEST_FILENAME
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            profile_metadata = manifest['entities']['user_profiles']
            profile_metadata['count'] = len(kept_lines)
            profile_metadata['sha256'] = sha256(profiles_path.read_bytes()).hexdigest()
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                encoding='utf-8',
                newline='\n',
            )

            report = validate_dataset(dataset)

            self.assertFalse(report.valid)
            self.assertNotIn('Checksum mismatch: user_profiles.jsonl', report.errors)
            self.assertNotIn(
                'Count mismatch for user_profiles: expected 2, found 1',
                report.errors,
            )
            missing_profile_records = [
                item
                for item in report.quarantined_records
                if item['entity'] == 'users' and item['pk'] == self.reporter.pk
            ]
            self.assertEqual(len(missing_profile_records), 1)
            self.assertIn(
                'user is missing a profile',
                missing_profile_records[0]['errors'],
            )
            with self.assertRaises(DataPortabilityError):
                import_dataset(dataset)

    def test_round_trip_preserves_a_legacy_overlong_profile_bio_verbatim(self):
        legacy_bio = '旧资料原文🙂' * 240
        UserProfile.objects.filter(user=self.author).update(bio=legacy_bio)

        with TemporaryDirectory() as temporary:
            dataset, _ = self.export_to(Path(temporary))
            report = validate_dataset(dataset)
            self.assertTrue(report.valid, report.as_dict())

            self.clear_portable_data()
            self.assertEqual(import_dataset(dataset), 'imported')

        self.assertEqual(
            User.objects.get(username='author').profile.bio,
            legacy_bio,
        )

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
            self.downgrade_to_v2(dataset)
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
                dataset.with_name(
                    f'{dataset.name}-{IMPORT_REPORT_FILENAME}'
                ).read_text(encoding='utf-8')
            )
            self.assertFalse(report_payload['valid'])
            self.assertTrue(report_payload['quarantined_records'])
            self.assertEqual(report_payload['status'], 'rolled_back')
            self.assertEqual(report_payload['database_state'], 'rolled_back')

    def test_v3_sequence_finalization_failure_is_resumable(self):
        if connection.vendor != 'sqlite':
            self.skipTest('SQLite failure injection; PostgreSQL runs in dedicated CI')
        from .services import data_portability

        deleted_share = Share.objects.create(
            title='sequence recovery high water',
            strategy_code='[stgy:sequence-recovery-high-water]',
            author=self.author,
        )
        deleted_pk = deleted_share.pk
        deleted_share.delete()
        with TemporaryDirectory() as temporary:
            dataset, manifest = self.export_to(Path(temporary))
            self.assertGreater(
                manifest['identity']['sequences']['shares']['next_value_floor'],
                deleted_pk,
            )
            source_hashes = {
                path.name: sha256(path.read_bytes()).hexdigest()
                for path in dataset.iterdir()
                if path.is_file()
            }
            self.clear_portable_data()
            with connection.cursor() as cursor:
                cursor.execute(
                    'UPDATE sqlite_sequence SET seq = 0 WHERE name = %s',
                    [Share._meta.db_table],
                )

            real_raise = data_portability._raise_sequence_floor

            def fail_share_sequence(spec, *args, **kwargs):
                if spec.name == 'shares':
                    raise RuntimeError('injected sequence finalization failure')
                return real_raise(spec, *args, **kwargs)

            with patch.object(
                data_portability,
                '_raise_sequence_floor',
                side_effect=fail_share_sequence,
            ):
                with self.assertRaisesRegex(
                    DataPortabilityError,
                    'sequence finalization failed',
                ):
                    import_dataset(dataset)

            self.assertTrue(User.objects.filter(username='author').exists())
            self.assertTrue(Share.objects.filter(pk=self.share.pk).exists())
            report_path = dataset.with_name(
                f'{dataset.name}-{IMPORT_REPORT_FILENAME}'
            )
            pending = json.loads(report_path.read_text(encoding='utf-8'))
            self.assertEqual(pending['status'], 'finalization_incomplete')
            self.assertEqual(pending['database_state'], 'content_committed')

            self.assertEqual(import_dataset(dataset), 'recovered')
            completed = json.loads(report_path.read_text(encoding='utf-8'))
            self.assertEqual(completed['status'], 'recovered')
            self.assertEqual(completed['database_state'], 'complete')
            self.assertEqual(
                source_hashes,
                {
                    path.name: sha256(path.read_bytes()).hexdigest()
                    for path in dataset.iterdir()
                    if path.is_file()
                },
            )

    def test_completed_database_can_recover_from_final_report_write_failure(self):
        from .services import data_portability

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset, manifest = self.export_to(root)
            self.clear_portable_data()
            first_report = root / 'evidence' / 'first-import.json'
            recovered_report = root / 'evidence' / 'recovered-import.json'
            real_write = data_portability._write_json_atomic

            def fail_completed_report(path, payload):
                if payload.get('status') == 'imported':
                    raise OSError('injected evidence write failure')
                return real_write(path, payload)

            with patch.object(
                data_portability,
                '_write_json_atomic',
                side_effect=fail_completed_report,
            ):
                with self.assertRaisesRegex(
                    DataPortabilityError,
                    'target database is complete',
                ):
                    import_dataset(dataset, report_path=first_report)

            self.assertTrue(database_matches_manifest(manifest))
            self.assertEqual(
                import_dataset(dataset, report_path=recovered_report),
                'already_imported',
            )
            evidence = json.loads(recovered_report.read_text(encoding='utf-8'))
            self.assertEqual(evidence['status'], 'already_imported')
            self.assertEqual(evidence['database_state'], 'complete')
            self.assertFalse(evidence['cutover_authorized'])

    def test_import_report_must_be_outside_the_immutable_dataset(self):
        with TemporaryDirectory() as temporary:
            dataset, _ = self.export_to(Path(temporary))
            self.clear_portable_data()

            with self.assertRaisesRegex(
                DataPortabilityError,
                'outside the immutable dataset',
            ):
                import_dataset(
                    dataset,
                    report_path=dataset / 'evidence' / 'import.json',
                )

            self.assertFalse(User.objects.exists())
            self.assertFalse(Share.objects.exists())

    def test_import_requires_exclusive_target_attestation(self):
        with TemporaryDirectory() as temporary:
            dataset, _ = self.export_to(Path(temporary))
            self.clear_portable_data()

            with self.assertRaisesRegex(
                DataPortabilityError,
                '--confirm-exclusive-target',
            ):
                _import_dataset(dataset)

            self.assertFalse(User.objects.exists())
            self.assertFalse(Share.objects.exists())

    def test_import_rejects_an_outer_transaction_before_writing_rows(self):
        with TemporaryDirectory() as temporary:
            dataset, _ = self.export_to(Path(temporary))
            self.clear_portable_data()

            with transaction.atomic():
                with self.assertRaisesRegex(
                    DataPortabilityError,
                    'existing transaction',
                ):
                    import_dataset(dataset)

            self.assertFalse(User.objects.exists())
            self.assertFalse(Share.objects.exists())

    def test_v3_rejects_noncanonical_and_structurally_malformed_jsonl(self):
        with TemporaryDirectory() as temporary:
            dataset, _ = self.export_to(Path(temporary))
            groups_path = dataset / 'groups.jsonl'
            record = json.loads(groups_path.read_text(encoding='utf-8'))
            record['pk'] = {'not': 'hashable'}
            record['fields']['permissions'] = None
            groups_path.write_text(
                json.dumps(record, ensure_ascii=False, sort_keys=False) + '\n',
                encoding='utf-8',
                newline='\n',
            )
            manifest_path = dataset / MANIFEST_FILENAME
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            manifest['entities']['groups']['sha256'] = sha256(
                groups_path.read_bytes()
            ).hexdigest()
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                encoding='utf-8',
                newline='\n',
            )

            report = validate_dataset(dataset)

        self.assertFalse(report.valid)
        errors = next(
            item['errors']
            for item in report.quarantined_records
            if item['entity'] == 'groups'
        )
        self.assertIn('record is not canonical v3 JSONL', errors)
        self.assertIn('primary key must be hashable', errors)
        self.assertIn('permissions must be a list', errors)


class DataPortabilityEmbeddedTableTests(TransactionTestCase):
    reset_sequences = True

    def test_orphaned_embedded_m2m_row_breaks_exact_match(self):
        if connection.vendor != 'sqlite':
            self.skipTest('SQLite constraint-toggle regression test')
        author = User.objects.create_user(username='embedded-author')
        share = Share.objects.create(
            title='embedded table integrity',
            strategy_code='[stgy:embedded-table-integrity]',
            author=author,
        )
        share.likes.add(author)

        with TemporaryDirectory() as temporary:
            manifest = export_dataset(Path(temporary) / 'dataset')
            self.assertTrue(database_matches_manifest(manifest))

            through = Share.likes.through
            quoted = connection.ops.quote_name
            constraints_disabled = connection.disable_constraint_checking()
            self.assertTrue(constraints_disabled)
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f'INSERT INTO {quoted(through._meta.db_table)} '
                        f'({quoted("share_id")}, {quoted("user_id")}) '
                        'VALUES (%s, %s)',
                        [99999991, 99999992],
                    )
                self.assertFalse(database_matches_manifest(manifest))
            finally:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f'DELETE FROM {quoted(through._meta.db_table)} '
                        f'WHERE {quoted("share_id")} = %s '
                        f'AND {quoted("user_id")} = %s',
                        [99999991, 99999992],
                    )
                connection.enable_constraint_checking()

    def test_manual_autocommit_off_boundary_is_rejected(self):
        author = User.objects.create_user(username='manual-transaction-author')
        Share.objects.create(
            title='manual transaction boundary',
            strategy_code='[stgy:manual-transaction-boundary]',
            author=author,
        )
        with TemporaryDirectory() as temporary:
            dataset = Path(temporary) / 'dataset'
            export_dataset(dataset)
            Share.objects.all().delete()
            User.objects.all().delete()

            connection.set_autocommit(False)
            try:
                with self.assertRaisesRegex(
                    DataPortabilityError,
                    'existing transaction',
                ):
                    import_dataset(dataset)
            finally:
                connection.rollback()
                connection.set_autocommit(True)

        self.assertFalse(User.objects.exists())
        self.assertFalse(Share.objects.exists())

    def test_commit_exception_is_reported_as_unknown_and_recoverable(self):
        author = User.objects.create_user(username='commit-unknown-author')
        Share.objects.create(
            title='commit unknown boundary',
            strategy_code='[stgy:commit-unknown-boundary]',
            author=author,
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / 'dataset'
            report_path = root / 'evidence' / 'commit-unknown.json'
            export_dataset(dataset)
            Share.objects.all().delete()
            User.objects.all().delete()

            real_exit = transaction.Atomic.__exit__

            def commit_then_disconnect(atomic, exc_type, exc_value, traceback):
                result = real_exit(atomic, exc_type, exc_value, traceback)
                if atomic.durable and exc_type is None:
                    raise DatabaseError('injected disconnect after commit')
                return result

            with patch.object(
                transaction.Atomic,
                '__exit__',
                new=commit_then_disconnect,
            ):
                with self.assertRaisesRegex(
                    DataPortabilityError,
                    'commit result is unknown',
                ):
                    import_dataset(dataset, report_path=report_path)

            self.assertTrue(User.objects.filter(
                username='commit-unknown-author'
            ).exists())
            evidence = json.loads(report_path.read_text(encoding='utf-8'))
            self.assertEqual(evidence['status'], 'commit_unknown')
            self.assertEqual(evidence['data_stage'], 'commit_unknown')
            self.assertIn(
                evidence['database_state'],
                {'content_committed', 'unknown'},
            )

            recovered_status = import_dataset(
                dataset,
                report_path=root / 'evidence' / 'recovered.json',
            )
            self.assertIn(recovered_status, {'recovered', 'already_imported'})


class DataPortabilityBackendLockTests(TransactionTestCase):
    def test_file_sqlite_exclusive_import_lock_blocks_a_native_second_connection(self):
        if connection.vendor != 'sqlite':
            self.skipTest('File-backed SQLite locking regression test')
        from .services import data_portability

        with TemporaryDirectory() as temporary:
            database_path = Path(temporary) / 'exclusive-lock.sqlite3'
            setup_connection = sqlite3.connect(database_path)
            try:
                setup_connection.execute('PRAGMA journal_mode=WAL')
                setup_connection.execute(
                    'CREATE TABLE lock_probe ('
                    'id INTEGER PRIMARY KEY, value TEXT NOT NULL)'
                )
                setup_connection.execute(
                    'INSERT INTO lock_probe(value) VALUES (?)',
                    ['before-lock'],
                )
                setup_connection.commit()
            finally:
                setup_connection.close()

            lock_settings = dict(connection.settings_dict)
            lock_settings['NAME'] = database_path
            lock_settings['OPTIONS'] = {'timeout': 0.05}
            lock_connection = SQLiteDatabaseWrapper(
                lock_settings,
                alias='sqlite_exclusive_lock_test',
            )
            competitor = None
            try:
                with patch.object(
                    data_portability,
                    'connection',
                    lock_connection,
                ):
                    with data_portability._exclusive_target_import_lock() as lock_kind:
                        self.assertEqual(lock_kind, 'sqlite_exclusive_locking_mode')
                        competitor = sqlite3.connect(database_path, timeout=0.05)
                        competitor.execute('PRAGMA busy_timeout=50')

                        with self.assertRaises(sqlite3.OperationalError) as read_error:
                            competitor.execute(
                                'SELECT value FROM lock_probe'
                            ).fetchall()
                        self.assertRegex(
                            str(read_error.exception).lower(),
                            r'locked|busy',
                        )

                        with self.assertRaises(sqlite3.OperationalError) as write_error:
                            competitor.execute(
                                'INSERT INTO lock_probe(value) VALUES (?)',
                                ['during-lock'],
                            )
                        self.assertRegex(
                            str(write_error.exception).lower(),
                            r'locked|busy',
                        )

                self.assertEqual(
                    competitor.execute(
                        'SELECT value FROM lock_probe ORDER BY id'
                    ).fetchall(),
                    [('before-lock',)],
                )
                competitor.execute(
                    'INSERT INTO lock_probe(value) VALUES (?)',
                    ['after-lock'],
                )
                competitor.commit()
                self.assertEqual(
                    competitor.execute(
                        'SELECT value FROM lock_probe ORDER BY id'
                    ).fetchall(),
                    [('before-lock',), ('after-lock',)],
                )
            finally:
                if competitor is not None:
                    competitor.close()
                lock_connection.close()


class DataPortabilityPostgreSQLBackendTests(TransactionTestCase):
    reset_sequences = True

    @staticmethod
    def _clear_portable_data():
        Session.objects.all().delete()
        LogEntry.objects.all().delete()
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

    @staticmethod
    def _postgres_sequence_floor(database_connection, sequence_name):
        from .services import data_portability

        with database_connection.cursor() as cursor:
            cursor.execute(
                'SELECT last_value, is_called FROM '
                f'{data_portability._quote_qualified_identifier(sequence_name)}'
            )
            last_value, is_called = cursor.fetchone()
        return int(last_value) + (1 if is_called else 0)

    def test_postgresql_sequence_finalization_resumes_after_second_setval_failure(self):
        if connection.vendor != 'postgresql':
            self.skipTest('PostgreSQL sequence recovery regression test')
        from .services import data_portability

        source_group = Group.objects.create(name='postgres-sequence-group')
        Group.objects.create(name='postgres-sequence-group-marker').delete()
        source_author = User.objects.create_user(
            username='postgres-sequence-author'
        )
        User.objects.create_user(
            username='postgres-sequence-user-marker'
        ).delete()
        source_share = Share.objects.create(
            title='PostgreSQL sequence recovery',
            strategy_code='[stgy:postgres-sequence-recovery]',
            author=source_author,
        )
        expected_primary_keys = {
            'groups': [source_group.pk],
            'users': [source_author.pk],
            'shares': [source_share.pk],
        }

        sequence_specs = [
            spec
            for spec in ENTITY_SPECS
            if isinstance(spec.model._meta.pk, models.AutoField)
        ]
        self.assertGreaterEqual(len(sequence_specs), 2)
        first_spec, second_spec = sequence_specs[:2]
        self.assertEqual(
            (first_spec.name, second_spec.name),
            ('groups', 'users'),
        )

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / 'dataset'
            manifest = export_dataset(dataset)
            required_floors = {
                spec.name: manifest['identity']['sequences'][spec.name][
                    'next_value_floor'
                ]
                for spec in (first_spec, second_spec)
            }
            sequence_names = {
                spec.name: data_portability._postgres_sequence_name(spec)
                for spec in (first_spec, second_spec)
            }

            self._clear_portable_data()
            with connection.cursor() as cursor:
                for sequence_name in sequence_names.values():
                    cursor.execute(
                        'SELECT setval(%s::regclass, %s, false)',
                        [sequence_name, 1],
                    )

            finalization_calls = []
            real_raise_sequence_floor = data_portability._raise_sequence_floor

            def fail_second_sequence(spec, *args, **kwargs):
                finalization_calls.append(spec.name)
                if len(finalization_calls) == 2:
                    raise RuntimeError('injected second PostgreSQL setval failure')
                return real_raise_sequence_floor(spec, *args, **kwargs)

            report_path = root / 'evidence' / 'first-import.json'
            with patch.object(
                data_portability,
                '_raise_sequence_floor',
                side_effect=fail_second_sequence,
            ):
                with self.assertRaisesRegex(
                    DataPortabilityError,
                    'sequence finalization failed',
                ):
                    import_dataset(dataset, report_path=report_path)

            self.assertEqual(finalization_calls[:2], ['groups', 'users'])
            evidence = json.loads(report_path.read_text(encoding='utf-8'))
            self.assertEqual(evidence['status'], 'finalization_incomplete')
            self.assertEqual(evidence['database_state'], 'content_committed')

            commit_probe = connection.copy(alias='postgres_sequence_commit_probe')
            try:
                quoted = commit_probe.ops.quote_name
                committed_primary_keys = {}
                with commit_probe.cursor() as cursor:
                    for name, model in (
                        ('groups', Group),
                        ('users', User),
                        ('shares', Share),
                    ):
                        cursor.execute(
                            f'SELECT {quoted(model._meta.pk.column)} '
                            f'FROM {quoted(model._meta.db_table)} '
                            f'ORDER BY {quoted(model._meta.pk.column)}'
                        )
                        committed_primary_keys[name] = [
                            row[0] for row in cursor.fetchall()
                        ]

                self.assertEqual(committed_primary_keys, expected_primary_keys)
                self.assertGreaterEqual(
                    self._postgres_sequence_floor(
                        commit_probe,
                        sequence_names[first_spec.name],
                    ),
                    required_floors[first_spec.name],
                )
                self.assertLess(
                    self._postgres_sequence_floor(
                        commit_probe,
                        sequence_names[second_spec.name],
                    ),
                    required_floors[second_spec.name],
                )
            finally:
                commit_probe.close()

            self.assertEqual(
                import_dataset(
                    dataset,
                    report_path=root / 'evidence' / 'recovered-import.json',
                ),
                'recovered',
            )
            self.assertEqual(
                list(Group.objects.order_by('pk').values_list('pk', flat=True)),
                expected_primary_keys['groups'],
            )
            self.assertEqual(
                list(User.objects.order_by('pk').values_list('pk', flat=True)),
                expected_primary_keys['users'],
            )
            self.assertEqual(
                list(Share.objects.order_by('pk').values_list('pk', flat=True)),
                expected_primary_keys['shares'],
            )
            self.assertTrue(database_matches_manifest(manifest))
            self.assertGreaterEqual(
                self._postgres_sequence_floor(
                    connection,
                    sequence_names[second_spec.name],
                ),
                required_floors[second_spec.name],
            )

    def test_postgresql_session_advisory_lock_is_mutually_exclusive(self):
        if connection.vendor != 'postgresql':
            self.skipTest('PostgreSQL advisory lock regression test')
        from .services import data_portability

        competitor = connection.copy(alias='postgres_advisory_lock_competitor')
        competitor_acquired = False
        try:
            with data_portability._exclusive_target_import_lock() as lock_kind:
                self.assertEqual(lock_kind, 'postgresql_session_advisory_lock')
                with competitor.cursor() as cursor:
                    cursor.execute(
                        'SELECT pg_try_advisory_lock(%s, %s)',
                        list(data_portability.POSTGRES_IMPORT_LOCK_KEYS),
                    )
                    self.assertFalse(cursor.fetchone()[0])

            with competitor.cursor() as cursor:
                cursor.execute(
                    'SELECT pg_try_advisory_lock(%s, %s)',
                    list(data_portability.POSTGRES_IMPORT_LOCK_KEYS),
                )
                competitor_acquired = bool(cursor.fetchone()[0])
                self.assertTrue(competitor_acquired)
        finally:
            if competitor_acquired:
                with competitor.cursor() as cursor:
                    cursor.execute(
                        'SELECT pg_advisory_unlock(%s, %s)',
                        list(data_portability.POSTGRES_IMPORT_LOCK_KEYS),
                    )
            competitor.close()
