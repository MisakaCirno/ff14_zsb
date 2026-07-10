from django.core.management.base import BaseCommand, CommandError

from shares.services.data_portability import DataPortabilityError, import_dataset


class Command(BaseCommand):
    help = 'Import a validated site-data dataset into an empty target database.'

    def add_arguments(self, parser):
        parser.add_argument('dataset_directory')
        parser.add_argument('--report')

    def handle(self, *args, **options):
        try:
            status = import_dataset(
                options['dataset_directory'],
                report_path=options['report'],
            )
        except (DataPortabilityError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        if status == 'already_imported':
            self.stdout.write(self.style.WARNING('Target already matches this dataset.'))
        else:
            self.stdout.write(self.style.SUCCESS('Dataset imported and verified.'))
