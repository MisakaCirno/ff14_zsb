import json
from io import StringIO
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase

from shares.services.deployment_schema import (
    DeploymentSchemaInspectionError,
    inspect_sqlite_deployment_schema,
)


class DeploymentSchemaInspectionTests(SimpleTestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / 'site.sqlite3'
        self.loader = MigrationLoader(None, ignore_no_migrations=True)
        self.leaves = sorted(self.loader.graph.leaf_nodes())
        self.expected_order = []
        seen = set()
        for leaf in self.leaves:
            for migration in self.loader.graph.forwards_plan(leaf):
                if migration not in seen:
                    seen.add(migration)
                    self.expected_order.append(migration)

    def create_history(self, applied):
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute(
                'CREATE TABLE django_migrations ('
                'id INTEGER PRIMARY KEY AUTOINCREMENT, '
                'app VARCHAR(255) NOT NULL, '
                'name VARCHAR(255) NOT NULL, '
                'applied DATETIME NOT NULL)'
            )
            connection.executemany(
                'INSERT INTO django_migrations (app, name, applied) '
                "VALUES (?, ?, '2026-07-20T00:00:00Z')",
                list(applied),
            )
            connection.commit()
        finally:
            connection.close()

    def inspect(self):
        return inspect_sqlite_deployment_schema(
            self.database_path,
            loader=self.loader,
        )

    def test_current_history_is_safe_to_start_and_unchanged(self):
        self.create_history(self.expected_order)
        before = self.database_path.stat()

        report = self.inspect()

        after = self.database_path.stat()
        self.assertEqual(report['status'], 'current')
        self.assertTrue(report['schema_current'])
        self.assertTrue(report['safe_to_start'])
        self.assertFalse(report['upgrade_required'])
        self.assertFalse(report['cutover_authorized'])
        self.assertEqual(report['pending_migrations'], [])
        self.assertEqual(before.st_size, after.st_size)
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
        for suffix in ('-wal', '-shm', '-journal'):
            self.assertFalse(Path(f'{self.database_path}{suffix}').exists())

    def test_missing_leaf_requires_upgrade_without_guessing(self):
        missing = ('shares', '0030_add_moderator_takedown')
        self.assertIn(missing, self.expected_order)
        self.create_history(
            migration for migration in self.expected_order if migration != missing
        )

        report = self.inspect()

        self.assertEqual(report['status'], 'upgrade_required')
        self.assertTrue(report['upgrade_required'])
        self.assertFalse(report['safe_to_start'])
        self.assertIn(list(missing), report['pending_migrations'])

    def test_unknown_applied_migration_invalidates_history(self):
        self.create_history([
            *self.expected_order,
            ('shares', '9999_unknown_production_migration'),
        ])

        report = self.inspect()

        self.assertEqual(report['status'], 'invalid_history')
        self.assertFalse(report['safe_to_start'])
        self.assertEqual(
            report['unknown_applied_migrations'],
            [['shares', '9999_unknown_production_migration']],
        )

    def test_applied_child_with_missing_parent_invalidates_history(self):
        missing_parent = ('shares', '0027_classify_legacy_private_shares')
        self.create_history(
            migration
            for migration in self.expected_order
            if migration != missing_parent
        )

        report = self.inspect()

        self.assertEqual(report['status'], 'invalid_history')
        self.assertIn(
            {
                'migration': ['shares', '0028_normalize_announcement_column_order'],
                'missing_parent': list(missing_parent),
            },
            report['dependency_gaps'],
        )

    def test_sidecar_refuses_inspection(self):
        self.create_history(self.expected_order)
        Path(f'{self.database_path}-wal').write_bytes(b'not-a-real-wal')

        with self.assertRaisesMessage(
            DeploymentSchemaInspectionError,
            'SQLite sidecars are present',
        ):
            self.inspect()

    def test_symbolic_link_database_is_rejected_when_supported(self):
        self.create_history(self.expected_order)
        link_path = self.database_path.with_name('linked.sqlite3')
        try:
            link_path.symlink_to(self.database_path)
        except OSError:
            self.skipTest('Symbolic links are not available in this environment.')

        with self.assertRaisesMessage(
            DeploymentSchemaInspectionError,
            'must not be a symbolic link',
        ):
            inspect_sqlite_deployment_schema(link_path, loader=self.loader)


class DeploymentSchemaCommandTests(SimpleTestCase):
    sqlite_databases = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': 'C:\\site.sqlite3',
        },
    }

    def current_report(self, **updates):
        report = {
            'format': 'ffxivshare-deployment-schema-status',
            'format_version': 1,
            'status': 'current',
            'read_only': True,
            'database_unchanged': True,
            'cutover_authorized': False,
            'database_path': 'C:\\site.sqlite3',
            'database_size': 1,
            'schema_current': True,
            'upgrade_required': False,
            'safe_to_start': True,
            'leaf_nodes': [],
            'expected_migration_count': 1,
            'applied_migration_count': 1,
            'pending_migrations': [],
            'unknown_applied_migrations': [],
            'dependency_gaps': [],
        }
        report.update(updates)
        return report

    @patch(
        'shares.management.commands.check_deployment_schema.'
        'inspect_sqlite_deployment_schema'
    )
    @patch(
        'shares.management.commands.check_deployment_schema.settings.DATABASES',
        sqlite_databases,
    )
    def test_require_current_allows_safe_database(self, inspect):
        inspect.return_value = self.current_report()

        output = []
        call_command(
            'check_deployment_schema',
            require_current=True,
            stdout=_ListWriter(output),
        )

        payload = json.loads(output[0])
        self.assertTrue(payload['safe_to_start'])

    @patch(
        'shares.management.commands.check_deployment_schema.'
        'inspect_sqlite_deployment_schema'
    )
    @patch(
        'shares.management.commands.check_deployment_schema.settings.DATABASES',
        sqlite_databases,
    )
    def test_require_current_rejects_pending_migrations(self, inspect):
        inspect.return_value = self.current_report(
            status='upgrade_required',
            schema_current=False,
            upgrade_required=True,
            safe_to_start=False,
            pending_migrations=[['shares', '0028_example']],
        )

        with self.assertRaisesMessage(CommandError, 'Database upgrade required'):
            call_command(
                'check_deployment_schema',
                require_current=True,
                stdout=StringIO(),
            )


class _ListWriter:
    def __init__(self, values):
        self.values = values

    def write(self, value, ending=None):
        self.values.append(value)
