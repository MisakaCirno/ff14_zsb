from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase

from .services.database_upgrade_semantics import (
    DatabaseUpgradeSemanticError,
    compare_sqlite_upgrade,
)


class DatabaseUpgradeSemanticTests(SimpleTestCase):
    @staticmethod
    def _create_database(path):
        with closing(sqlite3.connect(path)) as database:
            database.execute(
                'CREATE TABLE sample ('
                'id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL)'
            )
            database.execute(
                'INSERT INTO sample (value) VALUES (?)',
                ('must survive verbatim',),
            )
            database.commit()

    def _pair(self, root):
        source = root / 'source.sqlite3'
        candidate = root / 'candidate.sqlite3'
        self._create_database(source)
        shutil.copyfile(source, candidate)
        return source, candidate

    def test_exact_existing_rows_and_new_nullable_columns_pass(self):
        with TemporaryDirectory() as temporary:
            source, candidate = self._pair(Path(temporary))
            with closing(sqlite3.connect(candidate)) as database:
                database.execute('ALTER TABLE sample ADD COLUMN note TEXT')
                database.execute(
                    'CREATE TABLE shares_sitemessage ('
                    'id INTEGER PRIMARY KEY AUTOINCREMENT)'
                )
                database.commit()

            report = compare_sqlite_upgrade(source, candidate)

            self.assertEqual(report['status'], 'passed')
            self.assertEqual(report['tables']['sample']['source_rows'], 1)
            self.assertEqual(report['tables']['sample']['allowed_added_rows'], 0)
            self.assertTrue(report['checks']['preexisting_rows_preserved'])
            self.assertNotIn('must survive verbatim', str(report))

    def test_changed_existing_value_is_rejected(self):
        with TemporaryDirectory() as temporary:
            source, candidate = self._pair(Path(temporary))
            with closing(sqlite3.connect(candidate)) as database:
                database.execute(
                    'UPDATE sample SET value = ? WHERE id = 1',
                    ('silently changed',),
                )
                database.commit()

            with self.assertRaisesRegex(
                DatabaseUpgradeSemanticError,
                'changed a pre-existing row',
            ):
                compare_sqlite_upgrade(source, candidate)

    def test_deleted_existing_row_is_rejected(self):
        with TemporaryDirectory() as temporary:
            source, candidate = self._pair(Path(temporary))
            with closing(sqlite3.connect(candidate)) as database:
                database.execute('DELETE FROM sample WHERE id = 1')
                database.commit()

            with self.assertRaisesRegex(
                DatabaseUpgradeSemanticError,
                'lost 1 pre-existing row',
            ):
                compare_sqlite_upgrade(source, candidate)

    def test_unallowlisted_new_row_is_rejected(self):
        with TemporaryDirectory() as temporary:
            source, candidate = self._pair(Path(temporary))
            with closing(sqlite3.connect(candidate)) as database:
                database.execute(
                    'INSERT INTO sample (value) VALUES (?)',
                    ('unexpected row',),
                )
                database.commit()

            with self.assertRaisesRegex(
                DatabaseUpgradeSemanticError,
                'gained 1 unexpected row',
            ):
                compare_sqlite_upgrade(source, candidate)

    def test_lowered_sequence_floor_is_rejected(self):
        with TemporaryDirectory() as temporary:
            source, candidate = self._pair(Path(temporary))
            with closing(sqlite3.connect(candidate)) as database:
                database.execute(
                    "UPDATE sqlite_sequence SET seq = 0 WHERE name = 'sample'"
                )
                database.commit()

            with self.assertRaisesRegex(
                DatabaseUpgradeSemanticError,
                'sequence floor was lost',
            ):
                compare_sqlite_upgrade(source, candidate)

    def test_only_missing_users_receive_reviewed_profile_backfills(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source.sqlite3'
            candidate = root / 'candidate.sqlite3'
            joined = '2026-07-01 02:03:04.000000'
            with closing(sqlite3.connect(source)) as database:
                database.execute(
                    'CREATE TABLE auth_user ('
                    'id INTEGER PRIMARY KEY, date_joined TEXT NOT NULL)'
                )
                database.execute(
                    'CREATE TABLE shares_userprofile ('
                    'id INTEGER PRIMARY KEY AUTOINCREMENT, '
                    'nickname TEXT NOT NULL, bio TEXT NOT NULL, '
                    'home_feed_mode TEXT NOT NULL, created_at TEXT NOT NULL, '
                    'updated_at TEXT NOT NULL, user_id INTEGER NOT NULL UNIQUE)'
                )
                database.execute(
                    'INSERT INTO auth_user (id, date_joined) VALUES (1, ?)',
                    (joined,),
                )
                database.commit()
            shutil.copyfile(source, candidate)
            with closing(sqlite3.connect(candidate)) as database:
                database.execute(
                    'INSERT INTO shares_userprofile '
                    '(nickname, bio, home_feed_mode, created_at, updated_at, user_id) '
                    'VALUES (?, ?, ?, ?, ?, ?)',
                    ('', '', 'infinite', joined, joined, 1),
                )
                database.commit()

            report = compare_sqlite_upgrade(source, candidate)

            self.assertEqual(
                report['tables']['shares_userprofile']['allowed_added_rows'],
                1,
            )

    def test_recoverable_deletion_metadata_must_start_empty(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source.sqlite3'
            candidate = root / 'candidate.sqlite3'
            with closing(sqlite3.connect(source)) as database:
                database.execute(
                    'CREATE TABLE shares_share ('
                    'id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL)'
                )
                database.execute(
                    'INSERT INTO shares_share (title) VALUES (?)',
                    ('existing user content',),
                )
                database.commit()
            shutil.copyfile(source, candidate)
            with closing(sqlite3.connect(candidate)) as database:
                database.execute(
                    "ALTER TABLE shares_share ADD COLUMN deletion_origin "
                    "TEXT NOT NULL DEFAULT ''"
                )
                database.execute(
                    "UPDATE shares_share SET deletion_origin = 'owner'"
                )
                database.commit()

            with self.assertRaisesRegex(
                DatabaseUpgradeSemanticError,
                'deletion_origin has an unexpected migration value',
            ):
                compare_sqlite_upgrade(source, candidate)
