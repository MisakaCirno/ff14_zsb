from django.db import migrations


LEGACY_PRIVATE = 'legacy_private'
REPORT_TAKEDOWN = 'report_takedown'
CLEAR = 'clear'
LEGACY_PRIVATE_REASON = '历史私密状态来源待人工确认'

CONFIRM_ACTION = 'confirm_restriction'
RELEASE_ACTION = 'release_restriction'

DECISIONS = {
    '2k5d2w5w': {
        'action': CONFIRM_ACTION,
        'reason': 'R19 历史数据复核：旧管理后台记录显示管理员曾修改可见性，并标记为疑似重复内容。',
        'details': (
            'R19 历史状态人工复核：确认为历史下架并维持内容限制；'
            '依据为旧管理后台的可见性修改与疑似重复标记。'
        ),
    },
    '4s2v4e9n': {
        'action': RELEASE_ACTION,
        'details': (
            'R19 历史状态人工复核：确认为作者主动私密，解除迁移保护限制；'
            '可见性仍保持私有。'
        ),
    },
    '8b8y9s3j': {
        'action': RELEASE_ACTION,
        'details': (
            'R19 历史状态人工复核：确认为作者主动私密，解除迁移保护限制；'
            '可见性仍保持私有。'
        ),
    },
    '8n9b6e6b': {
        'action': RELEASE_ACTION,
        'details': (
            'R19 历史状态人工复核：确认为作者主动私密，解除迁移保护限制；'
            '可见性仍保持私有。'
        ),
    },
}


def _validate_legacy_private_share(share):
    if (
        share.status != 'approved'
        or share.visibility != 'private'
        or share.restriction_reason != LEGACY_PRIVATE_REASON
        or share.restricted_at is None
        or share.restricted_by_id is not None
    ):
        raise RuntimeError(
            f'R19 legacy-private decision precondition changed: {share.share_id}'
        )


def classify_legacy_private_shares(apps, schema_editor):
    Share = apps.get_model('shares', 'Share')
    ShareLog = apps.get_model('shares', 'ShareLog')

    for share in Share.objects.filter(
        share_id__in=DECISIONS,
    ).order_by('share_id'):
        if share.restriction_state != LEGACY_PRIVATE:
            continue
        _validate_legacy_private_share(share)
        decision = DECISIONS[share.share_id]
        action = decision['action']
        details = decision['details']
        if ShareLog.objects.filter(
            share_id=share.pk,
            action=action,
            details=details,
        ).exists():
            raise RuntimeError(
                f'R19 legacy-private decision log already exists: {share.share_id}'
            )

        if action == CONFIRM_ACTION:
            Share.objects.filter(pk=share.pk).update(
                restriction_state=REPORT_TAKEDOWN,
                restriction_reason=decision['reason'],
                restricted_at=share.updated_at,
                restricted_by_id=None,
            )
        else:
            Share.objects.filter(pk=share.pk).update(
                restriction_state=CLEAR,
                restriction_reason='',
                restricted_at=None,
                restricted_by_id=None,
            )

        log = ShareLog.objects.create(
            share_id=share.pk,
            user_id=None,
            action=action,
            details=details,
        )
        ShareLog.objects.filter(pk=log.pk).update(created_at=share.updated_at)


def restore_legacy_private_shares(apps, schema_editor):
    Share = apps.get_model('shares', 'Share')
    ShareLog = apps.get_model('shares', 'ShareLog')

    for share in Share.objects.filter(
        share_id__in=DECISIONS,
    ).order_by('share_id'):
        decision = DECISIONS[share.share_id]
        action = decision['action']
        details = decision['details']
        logs = ShareLog.objects.filter(
            share_id=share.pk,
            user_id=None,
            action=action,
            details=details,
        )
        log_count = logs.count()
        if log_count == 0:
            continue
        if log_count != 1:
            raise RuntimeError(
                f'R19 legacy-private decision log is not unique: {share.share_id}'
            )

        if action == CONFIRM_ACTION:
            expected = (
                share.restriction_state == REPORT_TAKEDOWN
                and share.restriction_reason == decision['reason']
                and share.restricted_at == share.updated_at
                and share.restricted_by_id is None
            )
        else:
            expected = (
                share.restriction_state == CLEAR
                and share.restriction_reason == ''
                and share.restricted_at is None
                and share.restricted_by_id is None
            )
        if not expected:
            raise RuntimeError(
                f'R19 legacy-private decision result changed: {share.share_id}'
            )

        logs.delete()
        Share.objects.filter(pk=share.pk).update(
            restriction_state=LEGACY_PRIVATE,
            restriction_reason=LEGACY_PRIVATE_REASON,
            restricted_at=share.updated_at,
            restricted_by_id=None,
        )


class Migration(migrations.Migration):
    dependencies = [
        ('shares', '0026_sync_announcement_permission_names'),
    ]

    operations = [
        migrations.RunPython(
            classify_legacy_private_shares,
            restore_legacy_private_shares,
        ),
    ]
