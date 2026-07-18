from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.test import SimpleTestCase, TransactionTestCase

from .models import Share
from .services.database_backup import (
    DatabaseBackupError,
    backup_sqlite_database,
    backup_sqlite_path,
)


class StandaloneSQLiteBackupTests(SimpleTestCase):
    application_version = '244c32734e9fab5af05bf544a654615eeab31404'

    @staticmethod
    def _create_source(path):
        path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(path)) as source:
            source.execute('PRAGMA foreign_keys = ON')
            source.execute(
                'CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT NOT NULL)'
            )
            source.execute('INSERT INTO sample (value) VALUES (?)', ('preserved',))
            source.commit()

    def test_path_backup_is_read_only_and_publishes_contract_sidecars(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'active-site' / 'db.sqlite3'
            output = root / 'r19-input' / 'Database' / 'production.sqlite3'
            self._create_source(source)
            source_digest = sha256(source.read_bytes()).hexdigest()

            result = backup_sqlite_path(
                source,
                output,
                application_version=self.application_version,
            )

            self.assertEqual(sha256(source.read_bytes()).hexdigest(), source_digest)
            self.assertEqual(Path(result['path']), output.resolve())
            metadata = json.loads(
                Path(result['metadata_path']).read_text(encoding='utf-8')
            )
            self.assertEqual(
                metadata['application_version'],
                self.application_version,
            )
            with closing(sqlite3.connect(output)) as snapshot:
                self.assertEqual(
                    snapshot.execute('SELECT value FROM sample').fetchone(),
                    ('preserved',),
                )

    def test_file_runs_as_isolated_stdlib_only_sidecar_tool(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'active site # 百分比%' / 'db # 数据库%.sqlite3'
            output = root / 'r19 输入 # 100%' / 'Database' / 'production.sqlite3'
            self._create_source(source)
            script = (
                Path(__file__).resolve().parent
                / 'services'
                / 'database_backup.py'
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    '-I',
                    '-S',
                    '-B',
                    '-X',
                    'utf8',
                    str(script),
                    str(source),
                    str(output),
                    '--application-version',
                    self.application_version,
                ],
                capture_output=True,
                check=False,
                encoding='utf-8',
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(Path(result['path']), output.resolve())
            self.assertTrue(output.is_file())
            self.assertTrue(Path(result['checksum_path']).is_file())
            self.assertTrue(Path(result['metadata_path']).is_file())

    def test_path_backup_rejects_live_tree_outputs_and_placeholder_release(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'active-site' / 'db.sqlite3'
            self._create_source(source)

            with self.assertRaisesRegex(
                DatabaseBackupError,
                'outside the live database directory',
            ):
                backup_sqlite_path(
                    source,
                    source.parent / 'backup.sqlite3',
                    application_version=self.application_version,
                )

            for placeholder in (
                'unknown',
                'immutable-production-release-id',
                'HEAD',
                'release\nbad',
            ):
                with self.subTest(placeholder=placeholder):
                    with self.assertRaisesRegex(
                        DatabaseBackupError,
                        'real immutable application version',
                    ):
                        backup_sqlite_path(
                            source,
                            root / 'r19-input' / 'production.sqlite3',
                            application_version=placeholder,
                        )

    def test_path_backup_includes_committed_wal_rows_without_touching_source(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'active WAL # 数据%' / 'db.sqlite3'
            output = root / 'r19 WAL 输入' / 'Database' / 'production.sqlite3'
            source.parent.mkdir(parents=True)

            with closing(sqlite3.connect(source)) as writer:
                self.assertEqual(
                    writer.execute('PRAGMA journal_mode = WAL').fetchone()[0],
                    'wal',
                )
                writer.execute('PRAGMA wal_autocheckpoint = 0')
                writer.execute(
                    'CREATE TABLE sample '
                    '(id INTEGER PRIMARY KEY, value TEXT NOT NULL)'
                )
                writer.commit()
                writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                writer.execute(
                    'INSERT INTO sample (value) VALUES (?)',
                    ('committed-only-in-wal',),
                )
                writer.commit()

                source_wal = source.with_name(f'{source.name}-wal')
                self.assertTrue(source_wal.is_file())
                database_digest = sha256(source.read_bytes()).hexdigest()
                wal_digest = sha256(source_wal.read_bytes()).hexdigest()

                backup_sqlite_path(
                    source,
                    output,
                    application_version=self.application_version,
                )

                self.assertEqual(sha256(source.read_bytes()).hexdigest(), database_digest)
                self.assertEqual(sha256(source_wal.read_bytes()).hexdigest(), wal_digest)
                with closing(sqlite3.connect(output)) as snapshot:
                    self.assertEqual(
                        snapshot.execute('SELECT value FROM sample').fetchall(),
                        [('committed-only-in-wal',)],
                    )
                    self.assertEqual(
                        snapshot.execute('PRAGMA integrity_check').fetchone(),
                        ('ok',),
                    )

                writer.execute(
                    'INSERT INTO sample (value) VALUES (?)',
                    ('writer-still-usable',),
                )
                writer.commit()

            self.assertFalse(output.with_name(f'{output.name}-wal').exists())
            self.assertFalse(output.with_name(f'{output.name}-shm').exists())
            self.assertFalse(output.with_name(f'{output.name}-journal').exists())

    def test_path_backup_rejects_hard_link_source_alias(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'active-site' / 'db.sqlite3'
            alias = root / 'aliases' / 'db.sqlite3'
            output = root / 'r19-input' / 'Database' / 'production.sqlite3'
            self._create_source(source)
            alias.parent.mkdir()
            os.link(source, alias)
            source_digest = sha256(source.read_bytes()).hexdigest()

            with self.assertRaisesRegex(DatabaseBackupError, 'hard-link aliases'):
                backup_sqlite_path(
                    alias,
                    output,
                    application_version=self.application_version,
                )

            self.assertEqual(sha256(source.read_bytes()).hexdigest(), source_digest)
            self.assertFalse(output.exists())

    @skipUnless(os.name == 'nt', 'Windows UNC path contract')
    def test_path_backup_rejects_unc_source_before_network_access(self):
        with TemporaryDirectory() as temporary:
            output = (
                Path(temporary).resolve()
                / 'r19-input'
                / 'Database'
                / 'production.sqlite3'
            )

            with self.assertRaisesRegex(DatabaseBackupError, 'non-UNC paths'):
                backup_sqlite_path(
                    r'\\untrusted-server\share\db.sqlite3',
                    output,
                    application_version=self.application_version,
                )

    def test_path_backup_rejects_symbolic_link_source(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'active-site' / 'db.sqlite3'
            alias = root / 'aliases' / 'db.sqlite3'
            output = root / 'r19-input' / 'Database' / 'production.sqlite3'
            self._create_source(source)
            alias.parent.mkdir()
            try:
                alias.symlink_to(source)
            except OSError as exc:
                self.skipTest(f'Symbolic links unavailable: {exc}')

            with self.assertRaisesRegex(DatabaseBackupError, 'symbolic link'):
                backup_sqlite_path(
                    alias,
                    output,
                    application_version=self.application_version,
                )

            self.assertFalse(output.exists())

    def test_no_overwrite_race_preserves_external_sentinel(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'active-site' / 'db.sqlite3'
            output = root / 'r19-input' / 'Database' / 'production.sqlite3'
            self._create_source(source)
            sentinel = b'RACE-SENTINEL-MUST-SURVIVE'
            real_link = os.link
            injected = False

            def inject_sentinel_before_first_publish(staged, destination):
                nonlocal injected
                if not injected and Path(destination) == output.resolve():
                    Path(destination).write_bytes(sentinel)
                    injected = True
                return real_link(staged, destination)

            with patch(
                'shares.services.database_backup.os.link',
                side_effect=inject_sentinel_before_first_publish,
            ):
                with self.assertRaisesRegex(
                    DatabaseBackupError,
                    'choose a completely new output path',
                ):
                    backup_sqlite_path(
                        source,
                        output,
                        application_version=self.application_version,
                    )

            self.assertTrue(injected)
            self.assertEqual(output.read_bytes(), sentinel)
            self.assertFalse(output.with_name(f'{output.name}.sha256').exists())
            self.assertFalse(output.with_name(f'{output.name}.metadata.json').exists())

    def test_partial_new_publication_never_deletes_final_names(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'active-site' / 'db.sqlite3'
            output = root / 'r19-input' / 'Database' / 'production.sqlite3'
            checksum_path = output.with_name(f'{output.name}.sha256')
            metadata_path = output.with_name(f'{output.name}.metadata.json')
            self._create_source(source)
            sentinel = b'EXTERNAL-CHECKSUM-SENTINEL'
            real_link = os.link
            injected = False

            def inject_checksum_conflict(staged, destination):
                nonlocal injected
                if not injected and Path(destination) == checksum_path.resolve():
                    Path(destination).write_bytes(sentinel)
                    injected = True
                return real_link(staged, destination)

            with patch(
                'shares.services.database_backup.os.link',
                side_effect=inject_checksum_conflict,
            ):
                with self.assertRaisesRegex(
                    DatabaseBackupError,
                    'choose a completely new output path',
                ):
                    backup_sqlite_path(
                        source,
                        output,
                        application_version=self.application_version,
                    )

            self.assertTrue(injected)
            self.assertTrue(output.exists())
            with closing(sqlite3.connect(output)) as partial_snapshot:
                self.assertEqual(
                    partial_snapshot.execute('PRAGMA integrity_check').fetchone(),
                    ('ok',),
                )
            self.assertEqual(checksum_path.read_bytes(), sentinel)
            self.assertFalse(metadata_path.exists())

    def test_concurrent_new_backups_publish_exactly_one_complete_set(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'active-site' / 'db.sqlite3'
            output = root / 'r19-input' / 'Database' / 'production.sqlite3'
            self._create_source(source)

            def run_backup():
                try:
                    return backup_sqlite_path(
                        source,
                        output,
                        application_version=self.application_version,
                    )
                except DatabaseBackupError as exc:
                    return exc

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _index: run_backup(), range(2)))

            successes = [item for item in results if isinstance(item, dict)]
            failures = [item for item in results if isinstance(item, DatabaseBackupError)]
            self.assertEqual(len(successes), 1, results)
            self.assertEqual(len(failures), 1, results)
            checksum_path = output.with_name(f'{output.name}.sha256')
            metadata_path = output.with_name(f'{output.name}.metadata.json')
            digest = sha256(output.read_bytes()).hexdigest()
            self.assertEqual(
                checksum_path.read_text(encoding='utf-8'),
                f'{digest}  {output.name}\n',
            )
            metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
            self.assertEqual(metadata['sha256'], digest)

    def test_failed_publication_cleans_temporary_sqlite_sidecars(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'active-site' / 'db.sqlite3'
            output = root / 'r19-input' / 'Database' / 'production.sqlite3'
            self._create_source(source)

            def create_sensitive_sidecars_then_fail(outputs, **_options):
                temporary_database = tuple(outputs)[0][0]
                for suffix in ('-wal', '-shm', '-journal'):
                    temporary_database.with_name(
                        f'{temporary_database.name}{suffix}'
                    ).write_bytes(b'sensitive-temporary-content')
                raise OSError('injected publication failure')

            with patch(
                'shares.services.database_backup._publish_output_set',
                side_effect=create_sensitive_sidecars_then_fail,
            ):
                with self.assertRaisesRegex(OSError, 'injected publication failure'):
                    backup_sqlite_path(
                        source,
                        output,
                        application_version=self.application_version,
                    )

            self.assertFalse(list(output.parent.glob('.*.tmp-*')))
            self.assertFalse(output.exists())


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
                'django.db.connections',
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
