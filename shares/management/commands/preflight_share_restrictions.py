import json
import os
from pathlib import Path
from uuid import uuid4

from django.core.management.base import BaseCommand, CommandError

from shares.services.restriction_preflight import (
    build_share_restriction_preflight,
)


def _write_report(path, payload, *, overwrite):
    destination = Path(path).expanduser().resolve()
    if destination.exists() and not overwrite:
        raise CommandError(
            f'Report already exists: {destination}. Use --overwrite explicitly.'
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f'.{destination.name}.tmp-{uuid4().hex}')
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
            encoding='utf-8',
            newline='\n',
        )
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


class Command(BaseCommand):
    help = '预检迁移后的分享审核限制，并输出可审计 JSON 报告。'

    def add_arguments(self, parser):
        parser.add_argument('--output', help='可选的 JSON 报告输出路径。')
        parser.add_argument(
            '--overwrite',
            action='store_true',
            help='显式允许覆盖已存在的报告。',
        )
        parser.add_argument(
            '--strict',
            action='store_true',
            help='将需要人工复核的历史时序歧义视为失败。',
        )

    def handle(self, *args, **options):
        report = build_share_restriction_preflight()
        rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
        if options['output']:
            destination = _write_report(
                options['output'],
                report,
                overwrite=options['overwrite'],
            )
            self.stdout.write(f'Wrote restriction preflight report: {destination}')
        else:
            self.stdout.write(rendered)

        if report['blocking_errors']:
            raise CommandError(
                'Share restriction preflight found blocking data inconsistencies.'
            )
        if options['strict'] and report['manual_review']['count']:
            raise CommandError(
                'Share restriction preflight requires manual review in strict mode.'
            )
        if report['manual_review']['count']:
            self.stderr.write(self.style.WARNING(
                '历史状态需要人工分类；正式切换前请处理报告中的全部 share_ids。'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                'Share restriction preflight passed.'
            ))
