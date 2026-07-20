import json
import os
from pathlib import Path
from uuid import uuid4

from django.core.management.base import BaseCommand, CommandError

from shares.services.database_upgrade_semantics import (
    DatabaseUpgradeSemanticError,
    compare_sqlite_upgrade,
)


def _write_report(path, payload):
    destination = Path(path).expanduser().resolve()
    if destination.exists():
        raise CommandError(f'Report already exists: {destination}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f'.{destination.name}.tmp-{uuid4().hex}')
    try:
        with temporary.open('w', encoding='utf-8', newline='\n') as stream:
            stream.write(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                + '\n'
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


class Command(BaseCommand):
    help = 'Prove that a migrated SQLite candidate preserved source user data.'

    def add_arguments(self, parser):
        parser.add_argument('source_database')
        parser.add_argument('candidate_database')
        parser.add_argument('--output', required=True)

    def handle(self, *args, **options):
        try:
            report = compare_sqlite_upgrade(
                options['source_database'],
                options['candidate_database'],
            )
            destination = _write_report(options['output'], report)
        except (DatabaseUpgradeSemanticError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f'Wrote database semantic report: {destination}')
        self.stdout.write(self.style.SUCCESS(
            'Database upgrade semantic comparison passed.'
        ))
