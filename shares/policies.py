from django.db.models import QuerySet

from .models import Collection, Share


def is_moderator(user):
    return bool(
        user
        and user.is_authenticated
        and (user.is_staff or user.is_superuser)
    )


def is_owner(user, obj):
    return bool(
        user
        and user.is_authenticated
        and obj.author_id is not None
        and obj.author_id == user.pk
    )


def can_view_share(user, share):
    """Apply the direct-link visibility policy for one share."""
    if is_moderator(user) or is_owner(user, share):
        return True
    if share.visibility == Share.Visibility.PRIVATE:
        return False
    if share.visibility not in {Share.Visibility.PUBLIC, Share.Visibility.UNLISTED}:
        return False
    return share.status in {Share.Status.APPROVED, Share.Status.PENDING}


def can_view_collection(user, collection):
    return collection.is_public or is_owner(user, collection) or is_moderator(user)


def public_share_queryset(queryset=None):
    queryset = queryset if queryset is not None else Share.objects.all()
    if not isinstance(queryset, QuerySet):
        raise TypeError('queryset must be a Django QuerySet')
    return queryset.filter(
        visibility=Share.Visibility.PUBLIC,
        status=Share.Status.APPROVED,
    )


def share_api_denial_status(share):
    """Hide moderation state while preserving the existing private-content 403 contract."""
    if share.visibility == Share.Visibility.PRIVATE:
        return 403
    return 404
