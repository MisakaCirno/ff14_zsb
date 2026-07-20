from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from shares.models import Collection, Share, ShareLog
from shares.policies import is_moderator, is_owner

from .audit import log_share_action


class ContentDeletionPermissionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ContentDeletionResult:
    content: Share | Collection
    changed: bool


@transaction.atomic
def move_share_to_trash(*, share_pk, actor):
    share = Share.objects.select_for_update().get(pk=share_pk)
    owner = is_owner(actor, share)
    moderator = is_moderator(actor)
    if not owner and not moderator:
        raise ContentDeletionPermissionError
    if share.is_deleted:
        return ContentDeletionResult(share, False)

    origin = (
        Share.DeletionOrigin.OWNER
        if owner
        else Share.DeletionOrigin.MODERATOR
    )
    reason = (
        '作者主动将分享移入回收站。'
        if origin == Share.DeletionOrigin.OWNER
        else '管理员将分享移入回收站。'
    )
    share.deleted_at = timezone.now()
    share.deleted_by = actor
    share.deletion_origin = origin
    share.deletion_reason = reason
    share.save(update_fields=[
        'deleted_at',
        'deleted_by',
        'deletion_origin',
        'deletion_reason',
        'updated_at',
    ])
    log_share_action(actor, share, ShareLog.ActionType.DELETE, reason)
    return ContentDeletionResult(share, True)


@transaction.atomic
def restore_share_from_trash(*, share_pk, actor):
    share = Share.objects.select_for_update().get(pk=share_pk)
    if not share.is_deleted:
        return ContentDeletionResult(share, False)
    owner_restore = (
        is_owner(actor, share)
        and share.deletion_origin == Share.DeletionOrigin.OWNER
    )
    if not owner_restore and not is_moderator(actor):
        raise ContentDeletionPermissionError

    previous_reason = share.deletion_reason
    share.deleted_at = None
    share.deleted_by = None
    share.deletion_origin = ''
    share.deletion_reason = ''
    share.save(update_fields=[
        'deleted_at',
        'deleted_by',
        'deletion_origin',
        'deletion_reason',
        'updated_at',
    ])
    log_share_action(
        actor,
        share,
        ShareLog.ActionType.RESTORE,
        f'从回收站恢复分享。原删除说明：{previous_reason}',
    )
    return ContentDeletionResult(share, True)


@transaction.atomic
def move_collection_to_trash(*, collection_pk, actor):
    collection = Collection.objects.select_for_update().get(pk=collection_pk)
    if not is_owner(actor, collection):
        raise ContentDeletionPermissionError
    if collection.deleted_at is not None:
        return ContentDeletionResult(collection, False)

    collection.deleted_at = timezone.now()
    collection.deleted_by = actor
    collection.deletion_reason = '作者主动将合集移入回收站。'
    collection.save(update_fields=[
        'deleted_at',
        'deleted_by',
        'deletion_reason',
        'updated_at',
    ])
    return ContentDeletionResult(collection, True)


@transaction.atomic
def restore_collection_from_trash(*, collection_pk, actor):
    collection = Collection.objects.select_for_update().get(pk=collection_pk)
    if not is_owner(actor, collection):
        raise ContentDeletionPermissionError
    if collection.deleted_at is None:
        return ContentDeletionResult(collection, False)

    collection.deleted_at = None
    collection.deleted_by = None
    collection.deletion_reason = ''
    collection.save(update_fields=[
        'deleted_at',
        'deleted_by',
        'deletion_reason',
        'updated_at',
    ])
    return ContentDeletionResult(collection, True)
