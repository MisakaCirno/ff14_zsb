import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from shares.services.deployment_schema import (
    DeploymentSchemaInspectionError,
    inspect_sqlite_deployment_schema,
)


class Command(BaseCommand):
    help = (
        'Read the SQLite migration history without using Django database '
        'connections and report whether the deployed code can start safely.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--require-current',
            action='store_true',
            help='Return a failure when unapplied migrations are present.',
        )

    def handle(self, *args, **options):
        database = settings.DATABASES['default']
        if database['ENGINE'] != 'django.db.backends.sqlite3':
            raise CommandError(
                'The direct Git deployment schema check currently supports '
                'SQLite only.'
            )
        try:
            report = inspect_sqlite_deployment_schema(database['NAME'])
        except DeploymentSchemaInspectionError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            json.dumps(report, ensure_ascii=False, sort_keys=True)
        )
        if report['status'] == 'invalid_history':
            raise CommandError(
                'The database migration history is inconsistent. Keep the '
                'application stopped and investigate before deployment.'
            )
        if options['require_current'] and report['upgrade_required']:
            pending = len(report['pending_migrations'])
            raise CommandError(
                f'Database upgrade required: {pending} migration(s) are '
                'pending. Run the approved maintenance upgrade workflow '
                'before starting Waitress.'
            )
