from django.db import migrations


PERMISSION_NAME_CHANGES = {
    'add_announcement': ('Can add 公告', 'Can add 站点动态'),
    'change_announcement': ('Can change 公告', 'Can change 站点动态'),
    'delete_announcement': ('Can delete 公告', 'Can delete 站点动态'),
    'view_announcement': ('Can view 公告', 'Can view 站点动态'),
}


def _sync_permission_names(apps, *, reverse=False):
    ContentType = apps.get_model('contenttypes', 'ContentType')
    Permission = apps.get_model('auth', 'Permission')
    content_type_id = ContentType.objects.filter(
        app_label='shares',
        model='announcement',
    ).values_list('pk', flat=True).first()
    if content_type_id is None:
        return

    for codename, names in PERMISSION_NAME_CHANGES.items():
        source_name, target_name = reversed(names) if reverse else names
        Permission.objects.filter(
            content_type_id=content_type_id,
            codename=codename,
            name=source_name,
        ).update(name=target_name)


def sync_announcement_permission_names(apps, schema_editor):
    _sync_permission_names(apps)


def restore_announcement_permission_names(apps, schema_editor):
    _sync_permission_names(apps, reverse=True)


class Migration(migrations.Migration):
    dependencies = [
        ('shares', '0025_add_collection_owner_index'),
    ]

    operations = [
        migrations.RunPython(
            sync_announcement_permission_names,
            restore_announcement_permission_names,
        ),
    ]
