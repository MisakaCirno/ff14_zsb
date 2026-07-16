from django.core.management.base import BaseCommand, CommandError

from shares.services.data_portability import DataPortabilityError, import_dataset


class Command(BaseCommand):
    help = 'Import a validated site-data dataset into an empty target database.'

    def add_arguments(self, parser):
        parser.add_argument('dataset_directory')
        parser.add_argument(
            '--report',
            required=True,
            help='External evidence path; it must be outside the immutable dataset.',
        )
        parser.add_argument(
            '--confirm-exclusive-target',
            action='store_true',
            required=True,
            help=(
                'Attest that every target application writer is stopped; the '
                'command also acquires a database-specific import mutex.'
            ),
        )

    def handle(self, *args, **options):
        try:
            status = import_dataset(
                options['dataset_directory'],
                report_path=options['report'],
                confirm_exclusive_target=options['confirm_exclusive_target'],
            )
        except (DataPortabilityError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        if status == 'already_imported':
            self.stdout.write(self.style.WARNING('Target already matches this dataset.'))
        elif status == 'recovered':
            self.stdout.write(self.style.SUCCESS(
                'Dataset content already matched; sequence finalization and evidence '
                'were recovered.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS('Dataset imported and verified.'))
