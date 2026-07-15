from django.db import migrations, models
from django.db.models.functions import Length


OLD_TITLE_MAX_LENGTH = 200
NEW_TITLE_MAX_LENGTH = 255


def _ensure_site_message_titles_fit(apps, schema_editor, *, max_length):
    SiteMessage = apps.get_model('shares', 'SiteMessage')
    oversized = SiteMessage.objects.using(schema_editor.connection.alias).annotate(
        title_length=Length('title'),
    ).filter(title_length__gt=max_length)
    if oversized.exists():
        sample_ids = list(oversized.values_list('pk', flat=True)[:10])
        raise RuntimeError(
            f'SiteMessage titles longer than {max_length} characters require a '
            f'lossless manual migration before continuing; sample ids: {sample_ids}'
        )


def ensure_site_message_titles_fit(apps, schema_editor):
    _ensure_site_message_titles_fit(
        apps,
        schema_editor,
        max_length=NEW_TITLE_MAX_LENGTH,
    )


def ensure_site_message_titles_fit_legacy(apps, schema_editor):
    _ensure_site_message_titles_fit(
        apps,
        schema_editor,
        max_length=OLD_TITLE_MAX_LENGTH,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('shares', '0023_userprofile_integrity'),
    ]

    operations = [
        migrations.RunPython(
            ensure_site_message_titles_fit,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AlterModelOptions(
            name='sitemessage',
            options={
                'ordering': ['-created_at', '-pk'],
                'verbose_name': '站内信',
                'verbose_name_plural': '站内信',
            },
        ),
        migrations.AlterField(
            model_name='sitemessage',
            name='title',
            field=models.CharField(max_length=255, verbose_name='标题'),
        ),
        migrations.RunPython(
            migrations.RunPython.noop,
            reverse_code=ensure_site_message_titles_fit_legacy,
        ),
    ]
