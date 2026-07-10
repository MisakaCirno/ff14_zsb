from contextlib import closing
from hashlib import sha256
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.contrib.auth.models import User
from django.db import connection
from django.test import TransactionTestCase

from .models import Share
from .services.database_backup import DatabaseBackupError, backup_sqlite_database


@skipUnless(connection.vendor == 'sqlite', 'SQLite-specific backup contract')
class SQLiteBackupTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        author = User.objects.create_user(username='backup-author')
        Share.objects.create(
            title='备份测试分享',
            strategy_code='[stgy:backup]',
            author=author,
        )

    def test_backup_is_readable_integrity_checked_and_hashed(self):
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / 'site-backup.sqlite3'

            result = backup_sqlite_database(output)

            self.assertTrue(output.is_file())
            checksum_path = Path(result['checksum_path'])
            self.assertTrue(checksum_path.is_file())
            digest = sha256(output.read_bytes()).hexdigest()
            self.assertEqual(result['sha256'], digest)
            self.assertEqual(
                checksum_path.read_text(encoding='utf-8'),
                f'{digest}  {output.name}\n',
            )
            with closing(sqlite3.connect(output)) as backup:
                self.assertEqual(
                    backup.execute('PRAGMA integrity_check').fetchone(),
                    ('ok',),
                )
                self.assertEqual(
                    backup.execute('SELECT COUNT(*) FROM shares_share').fetchone()[0],
                    1,
                )

    def test_existing_backup_requires_explicit_overwrite(self):
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / 'site-backup.sqlite3'
            first = backup_sqlite_database(output)

            with self.assertRaises(DatabaseBackupError):
                backup_sqlite_database(output)

            second = backup_sqlite_database(output, overwrite=True)
            self.assertEqual(first['sha256'], second['sha256'])
