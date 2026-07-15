from dataclasses import dataclass
from sqlite3 import SQLITE_LOCKED_SHAREDCACHE
from time import sleep

from django.db import IntegrityError, OperationalError, connection, transaction

from shares.models import Share
from shares.policies import can_view_share


class ShareInteractionUnavailableError(RuntimeError):
    """分享不存在，或当前用户不能再与其互动。"""


@dataclass(frozen=True, slots=True)
class ShareInteractionResult:
    share: Share
    is_active: bool
    count: int


SQLITE_LOCK_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.16)
INTERACTION_POLICY_FIELDS = (
    'author',
    'visibility',
    'status',
    'restriction_state',
)


def _ensure_interaction_is_allowed(user, share):
    if not can_view_share(user, share):
        raise ShareInteractionUnavailableError


@transaction.atomic
def _set_interaction_state_once(*, share_id, user, relation_name, target_active):
    """在短事务内把单个用户的互动关系收敛到显式目标状态。"""
    try:
        share = Share.objects.get(share_id=share_id)
    except Share.DoesNotExist as exc:
        raise ShareInteractionUnavailableError from exc

    _ensure_interaction_is_allowed(user, share)

    relation = getattr(share, relation_name)
    if target_active:
        relation.add(user)
    else:
        relation.remove(user)

    try:
        share.refresh_from_db(fields=INTERACTION_POLICY_FIELDS)
    except Share.DoesNotExist as exc:
        raise ShareInteractionUnavailableError from exc
    _ensure_interaction_is_allowed(user, share)

    return ShareInteractionResult(
        share=share,
        is_active=relation.filter(pk=user.pk).exists(),
        count=relation.count(),
    )


def _is_transient_sqlite_lock(exc):
    cause = exc.__cause__
    return (
        connection.vendor == 'sqlite'
        and cause is not None
        and getattr(cause, 'sqlite_errorcode', None) == SQLITE_LOCKED_SHAREDCACHE
    )


def _set_interaction_state(*, share_id, user, relation_name, target_active):
    for attempt in range(len(SQLITE_LOCK_RETRY_DELAYS) + 1):
        try:
            return _set_interaction_state_once(
                share_id=share_id,
                user=user,
                relation_name=relation_name,
                target_active=target_active,
            )
        except IntegrityError as exc:
            if not Share.objects.filter(share_id=share_id).exists():
                raise ShareInteractionUnavailableError from exc
            raise
        except OperationalError as exc:
            if (
                not _is_transient_sqlite_lock(exc)
                or attempt == len(SQLITE_LOCK_RETRY_DELAYS)
            ):
                raise
            sleep(SQLITE_LOCK_RETRY_DELAYS[attempt])


def set_like_state(*, share_id, user, target_active):
    return _set_interaction_state(
        share_id=share_id,
        user=user,
        relation_name='likes',
        target_active=target_active,
    )


def set_favorite_state(*, share_id, user, target_active):
    return _set_interaction_state(
        share_id=share_id,
        user=user,
        relation_name='favorites',
        target_active=target_active,
    )
