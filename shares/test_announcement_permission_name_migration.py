from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, tag


@tag('slow')
class AnnouncementPermissionNameMigrationTests(TransactionTestCase):
    migrate_from = ('shares', '0025_add_collection_owner_index')
    migrate_to = ('shares', '0026_sync_announcement_permission_names')

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        content_type, _ = ContentType.objects.get_or_create(
            app_label='shares',
            model='announcement',
        )
        self.permission_ids = {}
        for action in ('add', 'change', 'delete', 'view'):
            codename = f'{action}_announcement'
            permission, _ = Permission.objects.update_or_create(
                content_type_id=content_type.pk,
                codename=codename,
                defaults={'name': f'Can {action} 公告'},
            )
            self.permission_ids[codename] = permission.pk

        group = Group.objects.create(name='announcement-reviewers')
        group.permissions.add(
            Permission.objects.get(codename='change_announcement')
        )
        self.group_id = group.pk

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_updates_only_names_and_preserves_permission_relations(self):
        for action in ('add', 'change', 'delete', 'view'):
            codename = f'{action}_announcement'
            permission = Permission.objects.get(codename=codename)
            self.assertEqual(permission.pk, self.permission_ids[codename])
            self.assertEqual(permission.name, f'Can {action} 站点动态')

        group = Group.objects.get(pk=self.group_id)
        self.assertEqual(
            list(group.permissions.values_list('codename', flat=True)),
            ['change_announcement'],
        )
