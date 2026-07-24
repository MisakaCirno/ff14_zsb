from unittest import skipUnless

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, tag

from shares.models import Announcement


@tag('slow')
@skipUnless(connection.vendor == 'sqlite', 'SQLite-specific migration contract')
class AnnouncementStructureMigrationTests(TransactionTestCase):
    maxDiff = None
    migrate_from = ('shares', '0027_classify_legacy_private_shares')
    migrate_to = ('shares', '0028_normalize_announcement_column_order')
    sequence_floor = 9_600_006
    canonical_sql = (
        'CREATE TABLE "shares_announcement" '
        '("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, '
        '"title" varchar(200) NOT NULL, "content" text NOT NULL, '
        '"is_active" bool NOT NULL, "created_at" datetime NOT NULL, '
        '"updated_at" datetime NOT NULL)'
    )

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        announcement = Announcement.objects.create(
            title='R19 公告结构规范化',
            content='列顺序不同不应改变数据。',
        )
        self.announcement_id = announcement.pk

        with connection.cursor() as cursor:
            cursor.execute('DROP INDEX "announcement_active_idx"')
            cursor.execute(
                'CREATE TABLE "shares_announcement_r19_old" '
                '("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, '
                '"title" varchar(200) NOT NULL, "is_active" bool NOT NULL, '
                '"created_at" datetime NOT NULL, "updated_at" datetime NOT NULL, '
                '"content" text NOT NULL)'
            )
            cursor.execute(
                'INSERT INTO "shares_announcement_r19_old" '
                '("id", "title", "is_active", "created_at", "updated_at", '
                '"content") SELECT "id", "title", "is_active", "created_at", '
                '"updated_at", "content" FROM "shares_announcement"'
            )
            cursor.execute('DROP TABLE "shares_announcement"')
            cursor.execute(
                'ALTER TABLE "shares_announcement_r19_old" '
                'RENAME TO "shares_announcement"'
            )
            cursor.execute(
                'CREATE INDEX "announcement_active_idx" '
                'ON "shares_announcement" ("is_active", "created_at" DESC)'
            )
            cursor.execute(
                'UPDATE sqlite_sequence SET seq = %s WHERE name = %s',
                [self.sequence_floor, 'shares_announcement'],
            )
            self.assertEqual(cursor.rowcount, 1)

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_normalizes_column_order_without_losing_data_or_sequence_floor(self):
        announcement = Announcement.objects.get(pk=self.announcement_id)
        self.assertEqual(announcement.title, 'R19 公告结构规范化')
        self.assertEqual(announcement.content, '列顺序不同不应改变数据。')

        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT sql FROM sqlite_schema '
                'WHERE type = \'table\' AND name = \'shares_announcement\''
            )
            self.assertEqual(cursor.fetchone()[0], self.canonical_sql)
            cursor.execute('PRAGMA table_info("shares_announcement")')
            self.assertEqual(
                [row[1] for row in cursor.fetchall()],
                ['id', 'title', 'content', 'is_active', 'created_at', 'updated_at'],
            )
            cursor.execute(
                'SELECT seq FROM sqlite_sequence WHERE name = %s',
                ['shares_announcement'],
            )
            self.assertEqual(cursor.fetchone()[0], self.sequence_floor)
            cursor.execute(
                'SELECT COUNT(*) FROM sqlite_schema WHERE name = %s',
                ['shares_migration_0028_announcement_sequence_floor'],
            )
            self.assertEqual(cursor.fetchone()[0], 0)
