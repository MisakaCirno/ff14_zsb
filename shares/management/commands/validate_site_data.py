from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from shares.services.data_portability import (
    VALIDATION_REPORT_FILENAME,
    validate_dataset,
    write_validation_report,
)


class Command(BaseCommand):
    help = 'Validate a versioned site-data dataset and write a quarantine report.'

    def add_arguments(self, parser):
        parser.add_argument('dataset_directory')
        parser.add_argument('--report')

    def handle(self, *args, **options):
        dataset = Path(options['dataset_directory']).expanduser().resolve()
        report_path = (
            Path(options['report']).expanduser().resolve()
            if options['report']
            else (
                dataset / VALIDATION_REPORT_FILENAME
                if dataset.is_dir()
                else dataset.with_name(
                    f'{dataset.name}-{VALIDATION_REPORT_FILENAME}'
                )
            )
        )
        report = validate_dataset(dataset)
        write_validation_report(report, report_path)
        if not report.valid:
            raise CommandError(f'Dataset is invalid; see {report_path}')
        total = sum(report.entity_counts.values())
        self.stdout.write(self.style.SUCCESS(f'Validated {total} records.'))
