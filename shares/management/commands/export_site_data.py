from django.core.management.base import BaseCommand, CommandError

from shares.services.data_portability import DataPortabilityError, export_dataset


class Command(BaseCommand):
    help = 'Export all portable site data as a versioned JSONL dataset.'

    def add_arguments(self, parser):
        parser.add_argument('output_directory')
        parser.add_argument(
            '--overwrite',
            action='store_true',
            help='Replace an existing output directory explicitly.',
        )

    def handle(self, *args, **options):
        try:
            manifest = export_dataset(
                options['output_directory'],
                overwrite=options['overwrite'],
            )
        except (DataPortabilityError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        total = sum(item['count'] for item in manifest['entities'].values())
        self.stdout.write(self.style.SUCCESS(f'Exported {total} records.'))
