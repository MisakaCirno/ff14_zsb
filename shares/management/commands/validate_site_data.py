from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from shares.services.data_portability import (
    validate_dataset,
    write_validation_report,
)


class Command(BaseCommand):
    help = 'Validate a versioned site-data dataset and write a quarantine report.'

    def add_arguments(self, parser):
        parser.add_argument('dataset_directory')
        parser.add_argument(
            '--report',
            required=True,
            help='External validation evidence path outside the immutable dataset.',
        )

    def handle(self, *args, **options):
        dataset = Path(options['dataset_directory']).expanduser().resolve()
        report_path = Path(options['report']).expanduser().resolve()
        try:
            report_path.relative_to(dataset)
        except ValueError:
            pass
        else:
            raise CommandError(
                'Validation evidence must be stored outside the immutable dataset.'
            )
        report = validate_dataset(dataset)
        write_validation_report(report, report_path)
        if not report.valid:
            raise CommandError(f'Dataset is invalid; see {report_path}')
        total = sum(report.entity_counts.values())
        self.stdout.write(self.style.SUCCESS(f'Validated {total} records.'))
