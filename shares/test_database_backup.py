from contextlib import closing
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.test import TransactionTestCase

from .models import Share
from .services.database_backup import DatabaseBackupError, backup_sqlite_database


@skipUnless(connection.vendor == 'sqlite', 'SQLite-specific backup contract')
class SQLiteBackupTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.author = User.objects.create_user(username='backup-author')
        self.share = Share.objects.create(
            title='备份测试分享',
            strategy_code='[stgy:backup]',
            author=self.author,
        )

    @staticmethod
    def _output_paths(output):
        return (
            output,
            output.with_name(f'{output.name}.sha256'),
            output.with_name(f'{output.name}.metadata.json'),
        )

    def test_backup_is_readable_integrity_checked_and_hashed(self):
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / 'site-backup.sqlite3'

            with patch.dict(os.environ, {'APP_VERSION': 'backup-test-release'}):
                result = backup_sqlite_database(output)

            self.assertTrue(output.is_file())
            checksum_path = Path(result['checksum_path'])
            metadata_path = Path(result['metadata_path'])
            self.assertTrue(checksum_path.is_file())
            self.assertTrue(metadata_path.is_file())
            digest = sha256(output.read_bytes()).hexdigest()
            self.assertEqual(result['sha256'], digest)
            self.assertEqual(
                checksum_path.read_text(encoding='utf-8'),
                f'{digest}  {output.name}\n',
            )
            metadata_text = metadata_path.read_text(encoding='utf-8')
            metadata = json.loads(metadata_text)
            self.assertEqual(metadata['schema_version'], 1)
            self.assertEqual(metadata['backup_method'], 'sqlite_backup_api')
            self.assertEqual(metadata['database_vendor'], 'sqlite')
            self.assertEqual(metadata['application_version'], 'backup-test-release')
            self.assertEqual(metadata['sha256'], digest)
            self.assertEqual(metadata['size'], output.stat().st_size)
            self.assertEqual(metadata['integrity_check'], 'ok')
            self.assertEqual(metadata['foreign_key_check'], 'ok')
            self.assertTrue(metadata['generated_at'].endswith('Z'))
            generated_at = datetime.fromisoformat(
                metadata['generated_at'].replace('Z', '+00:00')
            )
            self.assertIsNotNone(generated_at.tzinfo)
            self.assertNotIn('source', metadata)
            self.assertNotIn(str(connection.settings_dict['NAME']), metadata_text)
            with closing(sqlite3.connect(output)) as backup:
                self.assertEqual(
                    backup.execute('PRAGMA integrity_check').fetchone(),
                    ('ok',),
                )
                self.assertEqual(
                    backup.execute('SELECT COUNT(*) FROM shares_share').fetchone()[0],
                    1,
                )
                self.assertIsNone(
                    backup.execute('PRAGMA foreign_key_check').fetchone()
                )

    def test_any_existing_output_requires_explicit_overwrite(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, existing_index in enumerate(range(3)):
                with self.subTest(existing_index=existing_index):
                    output = root / str(index) / 'site-backup.sqlite3'
                    output.parent.mkdir()
                    paths = self._output_paths(output)
                    paths[existing_index].write_bytes(b'existing')

                    with self.assertRaises(DatabaseBackupError):
                        backup_sqlite_database(output)

                    self.assertEqual(paths[existing_index].read_bytes(), b'existing')
                    self.assertEqual(
                        [path.exists() for path in paths],
                        [item == existing_index for item in range(3)],
                    )

    def test_overwrite_refuses_live_database_and_journal_sidecars(self):
        with TemporaryDirectory() as temporary:
            live_database = Path(temporary) / 'live.sqlite3'
            sentinel = b'active SQLite file'

            class DatabaseListCursor:
                @staticmethod
                def fetchall():
                    return [(0, 'main', str(live_database))]

            class SourceConnection:
                @staticmethod
                def execute(statement):
                    if statement != 'PRAGMA database_list':
                        raise AssertionError(f'Unexpected statement: {statement}')
                    return DatabaseListCursor()

                @staticmethod
                def backup(*_args, **_kwargs):
                    raise AssertionError('A protected output reached the backup step.')

            class DatabaseConnection:
                vendor = 'sqlite'
                settings_dict = {
                    'NAME': 'file:misleading-name.sqlite3?mode=rwc',
                }
                connection = SourceConnection()

                @staticmethod
                def ensure_connection():
                    return None

            with patch(
                'shares.services.database_backup.connections',
                {'default': DatabaseConnection()},
            ):
                for suffix in ('', '-wal', '-shm', '-journal'):
                    with self.subTest(suffix=suffix):
                        output = live_database.with_name(
                            f'{live_database.name}{suffix}'
                        )
                        output.write_bytes(sentinel)

                        with self.assertRaisesRegex(
                            DatabaseBackupError,
                            'live SQLite database and its journal sidecars',
                        ):
                            backup_sqlite_database(output, overwrite=True)

                        self.assertEqual(output.read_bytes(), sentinel)
                        for sidecar in self._output_paths(output)[1:]:
                            self.assertFalse(sidecar.exists())
                        output.unlink()

    def test_overwrite_consistently_replaces_all_three_outputs(self):
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / 'site-backup.sqlite3'
            backup_sqlite_database(output)
            output_path, checksum_path, metadata_path = self._output_paths(output)
            output_path.write_bytes(b'stale database')
            checksum_path.write_text('stale checksum\n', encoding='utf-8')
            metadata_path.write_text('{"stale": true}\n', encoding='utf-8')

            with patch.dict(os.environ, {'APP_VERSION': 'replacement-release'}):
                result = backup_sqlite_database(output, overwrite=True)

            digest = sha256(output_path.read_bytes()).hexdigest()
            self.assertEqual(result['sha256'], digest)
            self.assertEqual(
                checksum_path.read_text(encoding='utf-8'),
                f'{digest}  {output.name}\n',
            )
            metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
            self.assertEqual(metadata['application_version'], 'replacement-release')
            self.assertEqual(metadata['sha256'], digest)
            self.assertEqual(metadata['size'], output_path.stat().st_size)
            self.assertFalse(list(output.parent.glob('.*.tmp-*')))
            self.assertFalse(list(output.parent.glob('.*.rollback-*')))

    def test_foreign_key_failure_does_not_publish_or_replace_outputs(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            existing_output = root / 'existing.sqlite3'
            backup_sqlite_database(existing_output)
            existing_paths = self._output_paths(existing_output)
            existing_contents = [path.read_bytes() for path in existing_paths]
            fresh_output = root / 'fresh.sqlite3'

            connection.disable_constraint_checking()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        'UPDATE shares_share SET author_id = %s WHERE id = %s',
                        [999999999, self.share.pk],
                    )
            finally:
                connection.enable_constraint_checking()

            try:
                with self.assertRaisesRegex(
                    DatabaseBackupError,
                    'foreign key check failed',
                ):
                    backup_sqlite_database(fresh_output)
                self.assertFalse(any(
                    path.exists() for path in self._output_paths(fresh_output)
                ))

                with self.assertRaisesRegex(
                    DatabaseBackupError,
                    'foreign key check failed',
                ):
                    backup_sqlite_database(existing_output, overwrite=True)
                self.assertEqual(
                    [path.read_bytes() for path in existing_paths],
                    existing_contents,
                )
            finally:
                with connection.cursor() as cursor:
                    cursor.execute(
                        'UPDATE shares_share SET author_id = %s WHERE id = %s',
                        [self.author.pk, self.share.pk],
                    )

    def test_publication_failure_rolls_back_the_complete_output_set(self):
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / 'site-backup.sqlite3'
            backup_sqlite_database(output)
            output_paths = self._output_paths(output)
            original_contents = [path.read_bytes() for path in output_paths]
            metadata_path = output_paths[2]
            real_replace = os.replace

            def fail_metadata_publication(source, destination):
                source_path = Path(source)
                destination_path = Path(destination)
                if (
                    destination_path.name == metadata_path.name
                    and '.tmp-' in source_path.name
                ):
                    raise OSError('injected metadata publication failure')
                return real_replace(source, destination)

            with patch(
                'shares.services.database_backup.os.replace',
                side_effect=fail_metadata_publication,
            ):
                with self.assertRaisesRegex(
                    OSError,
                    'injected metadata publication failure',
                ):
                    backup_sqlite_database(output, overwrite=True)

            self.assertEqual(
                [path.read_bytes() for path in output_paths],
                original_contents,
            )
            self.assertFalse(list(output.parent.glob('.*.tmp-*')))
            self.assertFalse(list(output.parent.glob('.*.rollback-*')))
