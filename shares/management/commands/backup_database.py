import sqlite3

from django.core.management.base import BaseCommand, CommandError

from shares.services.database_backup import DatabaseBackupError, backup_sqlite_database


class Command(BaseCommand):
    help = (
        'Create an online SQLite backup validated by integrity and foreign-key '
        'checks, with SHA-256 and JSON metadata sidecars.'
    )

    def add_arguments(self, parser):
        parser.add_argument('output_file')
        parser.add_argument(
            '--overwrite',
            action='store_true',
            help=(
                'Atomically replace an existing backup, checksum, and JSON '
                'metadata sidecar.'
            ),
        )

    def handle(self, *args, **options):
        try:
            result = backup_sqlite_database(
                options['output_file'],
                overwrite=options['overwrite'],
            )
        except (DatabaseBackupError, OSError, sqlite3.Error) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            f'Backup created: {result["path"]} ({result["size"]} bytes); '
            f'checksum: {result["checksum_path"]}; '
            f'metadata: {result["metadata_path"]}'
        ))
