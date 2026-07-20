import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError


def prevent_lossy_reverse(apps, schema_editor):
    database = schema_editor.connection.alias
    Share = apps.get_model('shares', 'Share')
    Collection = apps.get_model('shares', 'Collection')
    if (
        Share.objects.using(database).filter(deleted_at__isnull=False).exists()
        or Collection.objects.using(database).filter(
            deleted_at__isnull=False,
        ).exists()
    ):
        raise IrreversibleError(
            'Cannot reverse recoverable deletion fields while the recycle bin '
            'contains user content. Restore the pre-migration database backup '
            'instead of dropping deletion metadata.'
        )


class Migration(migrations.Migration):
    atomic = True

    dependencies = [
        ('shares', '0028_normalize_announcement_column_order'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='share',
            name='deleted_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='移入回收站时间'),
        ),
        migrations.AddField(
            model_name='share',
            name='deleted_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='deleted_shares', to=settings.AUTH_USER_MODEL, verbose_name='删除操作人'),
        ),
        migrations.AddField(
            model_name='share',
            name='deletion_origin',
            field=models.CharField(blank=True, choices=[('owner', '作者删除'), ('moderator', '管理员删除')], default='', max_length=10, verbose_name='删除来源'),
        ),
        migrations.AddField(
            model_name='share',
            name='deletion_reason',
            field=models.TextField(blank=True, verbose_name='删除说明'),
        ),
        migrations.AddField(
            model_name='collection',
            name='deleted_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='移入回收站时间'),
        ),
        migrations.AddField(
            model_name='collection',
            name='deleted_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='deleted_collections', to=settings.AUTH_USER_MODEL, verbose_name='删除操作人'),
        ),
        migrations.AddField(
            model_name='collection',
            name='deletion_reason',
            field=models.TextField(blank=True, verbose_name='删除说明'),
        ),
        migrations.AlterField(
            model_name='sharelog',
            name='action',
            field=models.CharField(choices=[('create', '创建分享'), ('edit', '编辑分享'), ('approve', '审核通过'), ('reject', '审核拒绝'), ('confirm_restriction', '确认维持内容限制'), ('release_restriction', '解除内容限制'), ('add_collection', '加入合集'), ('remove_collection', '移出合集'), ('report_handle', '处理举报'), ('delete', '删除分享'), ('restore', '恢复分享'), ('other', '其他操作')], max_length=20, verbose_name='操作类型'),
        ),
        migrations.AddIndex(
            model_name='share',
            index=models.Index(fields=['deleted_at', '-created_at'], name='share_deleted_idx'),
        ),
        migrations.AddIndex(
            model_name='collection',
            index=models.Index(fields=['deleted_at', '-updated_at'], name='collection_deleted_idx'),
        ),
        migrations.AddConstraint(
            model_name='share',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('deleted_at__isnull', True), ('deleted_by__isnull', True), ('deletion_origin', ''), ('deletion_reason', '')), models.Q(('deleted_at__isnull', False), ('deletion_origin__in', ['owner', 'moderator']), models.Q(('deletion_reason', ''), _negated=True)), _connector='OR'), name='share_deletion_metadata'),
        ),
        migrations.AddConstraint(
            model_name='collection',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('deleted_at__isnull', True), ('deleted_by__isnull', True), ('deletion_reason', '')), models.Q(('deleted_at__isnull', False), models.Q(('deletion_reason', ''), _negated=True)), _connector='OR'), name='collection_deletion_metadata'),
        ),
        migrations.RunPython(
            migrations.RunPython.noop,
            prevent_lossy_reverse,
        ),
    ]
